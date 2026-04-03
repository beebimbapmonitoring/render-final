from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, AsyncIterator, Optional
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from starlette.websockets import WebSocketDisconnect, WebSocketState

from crud import create_event, list_events as crud_list_events
from db import Base, engine, get_db
from models import EventLog
from schema import EventIn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("render-proxy")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEFAULT_PI_HTTP_BASE = os.getenv("PI_HTTP_BASE", "").strip().rstrip("/")
PI_API_TOKEN = os.getenv("PI_API_TOKEN", "").strip()
RPI_EVENT_TOKEN = os.getenv("RPI_EVENT_TOKEN", "").strip()

PI_HTTP_BASE_RUNTIME = DEFAULT_PI_HTTP_BASE

app = FastAPI(title="Hive Render Proxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event() -> None:
    Base.metadata.create_all(bind=engine)


@app.middleware("http")
async def always_cors(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception as exc:
        logger.exception("Unhandled HTTP proxy error")
        resp = JSONResponse({"ok": False, "error": str(exc)}, status_code=500)

    origin = request.headers.get("origin", "*")
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-admin-token, x-rpi-token, Authorization"
    return resp


@app.options("/{path:path}")
async def preflight(path: str):
    return Response(status_code=204)


def _validate_url(new_base: str) -> None:
    parsed = urlparse(new_base)
    if not parsed.scheme or not parsed.netloc:
        raise ValueError("Invalid URL")
    if any(ch.isspace() for ch in parsed.netloc):
        raise ValueError("Invalid host")


def _pi_http_base() -> str:
    base = (PI_HTTP_BASE_RUNTIME or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PI_HTTP_BASE not set. Call /admin/set-pi.")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError("PI_HTTP_BASE must start with http:// or https://")
    return base


def _pi_ws_base() -> str:
    base = _pi_http_base()
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://")
    return "ws://" + base.removeprefix("http://")


def _auth_headers() -> dict[str, str]:
    if not PI_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {PI_API_TOKEN}"}


def _require_rpi_event_token(x_rpi_token: str | None) -> None:
    if not RPI_EVENT_TOKEN:
        return
    if x_rpi_token != RPI_EVENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _passthrough_response_headers(source_headers: httpx.Headers) -> dict[str, str]:
    allowed = {
        "content-type",
        "cache-control",
        "pragma",
        "expires",
        "connection",
        "x-accel-buffering",
        "x-audio-format",
        "x-audio-rate",
        "x-audio-channels",
    }
    headers: dict[str, str] = {}
    for key, value in source_headers.items():
        if key.lower() in allowed:
            headers[key] = value
    return headers


@app.get("/__routes")
async def debug_routes() -> dict[str, list[dict[str, Any]]]:
    routes = []
    for route in app.router.routes:
        path = getattr(route, "path", str(route))
        methods = sorted(getattr(route, "methods", []) or [])
        routes.append({"path": path, "methods": methods})
    return {"routes": routes}


@app.get("/health")
async def health(db: Session = Depends(get_db)) -> dict:
    db_ok = False
    try:
        db.execute(EventLog.__table__.select().limit(1))
        db_ok = True
    except Exception:
        db_ok = False

    return {
        "ok": True,
        "pi_http_base": PI_HTTP_BASE_RUNTIME or None,
        "has_admin_token": bool(ADMIN_TOKEN),
        "has_pi_api_token": bool(PI_API_TOKEN),
        "has_rpi_event_token": bool(RPI_EVENT_TOKEN),
        "db_ok": db_ok,
    }


@app.post("/admin/set-pi")
async def admin_set_pi(payload: dict, request: Request) -> Response:
    global PI_HTTP_BASE_RUNTIME

    if not ADMIN_TOKEN:
        return Response("ADMIN_TOKEN not configured", status_code=500)

    token = request.headers.get("x-admin-token", "")
    if token != ADMIN_TOKEN:
        return Response("Unauthorized", status_code=401)

    raw = str(payload.get("pi_http_base", "")).strip()
    new_base = raw.rstrip("/")

    if not new_base.startswith(("http://", "https://")):
        return Response("pi_http_base must start with http:// or https://", status_code=400)

    try:
        _validate_url(new_base)
    except ValueError as exc:
        return Response(str(exc), status_code=400)

    PI_HTTP_BASE_RUNTIME = new_base
    return JSONResponse({"ok": True, "pi_http_base": PI_HTTP_BASE_RUNTIME})


async def _forward_get(path: str, timeout_s: float = 12.0) -> Response:
    url = f"{_pi_http_base()}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        response = await client.get(url, headers=_auth_headers())

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/octet-stream"),
        headers=_passthrough_response_headers(response.headers),
    )


async def _forward_post(path: str, payload: Any = None, timeout_s: float = 12.0) -> Response:
    url = f"{_pi_http_base()}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        response = await client.post(url, json=payload, headers=_auth_headers())

    return Response(
        content=response.content,
        status_code=response.status_code,
        media_type=response.headers.get("content-type", "application/json"),
        headers=_passthrough_response_headers(response.headers),
    )


async def _stream_upstream(url: str) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(connect=12, read=None, write=12, pool=12)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=_auth_headers()) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk


async def _stream_upstream_with_headers(url: str) -> tuple[AsyncIterator[bytes], dict[str, str], str]:
    timeout = httpx.Timeout(connect=12, read=None, write=12, pool=12)
    client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)
    response = await client.stream("GET", url, headers=_auth_headers()).__aenter__()
    try:
        response.raise_for_status()
    except Exception:
        await response.aclose()
        await client.aclose()
        raise

    headers = _passthrough_response_headers(response.headers)
    media_type = response.headers.get("content-type", "application/octet-stream")

    async def gen() -> AsyncIterator[bytes]:
        try:
            async for chunk in response.aiter_bytes():
                if chunk:
                    yield chunk
        finally:
            await response.aclose()
            await client.aclose()

    return gen(), headers, media_type


@app.get("/api/latest")
async def api_latest() -> Response:
    return await _forward_get("/api/latest")


@app.get("/api/sensors")
async def api_sensors() -> Response:
    return await _forward_get("/api/sensors")


@app.get("/video.mjpg")
async def video_mjpg() -> StreamingResponse:
    url = f"{_pi_http_base()}/video.mjpg"
    return StreamingResponse(
        _stream_upstream(url),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/audio")
async def audio_http() -> Response:
    return await _forward_get("/audio", timeout_s=60.0)


@app.get("/audio/stream")
async def audio_stream() -> StreamingResponse:
    url = f"{_pi_http_base()}/audio/stream"
    body, headers, media_type = await _stream_upstream_with_headers(url)
    headers.setdefault("Cache-Control", "no-cache, no-store, must-revalidate")
    headers.setdefault("Pragma", "no-cache")
    headers.setdefault("Expires", "0")
    headers.setdefault("Connection", "keep-alive")
    headers.setdefault("X-Accel-Buffering", "no")
    return StreamingResponse(body, media_type=media_type, headers=headers)


@app.get("/audio/stream/")
async def audio_stream_slash() -> StreamingResponse:
    return await audio_stream()


@app.get("/api/audio/stream")
async def api_audio_stream() -> StreamingResponse:
    url = f"{_pi_http_base()}/api/audio/stream"
    body, headers, media_type = await _stream_upstream_with_headers(url)
    headers.setdefault("Cache-Control", "no-cache, no-store, must-revalidate")
    headers.setdefault("Pragma", "no-cache")
    headers.setdefault("Expires", "0")
    headers.setdefault("Connection", "keep-alive")
    headers.setdefault("X-Accel-Buffering", "no")
    return StreamingResponse(body, media_type=media_type, headers=headers)


@app.get("/api/audio/stream/")
async def api_audio_stream_slash() -> StreamingResponse:
    return await api_audio_stream()


@app.post("/api/video/open")
async def api_video_open() -> Response:
    return await _forward_post("/api/video/open")


@app.post("/api/video/close")
async def api_video_close() -> Response:
    return await _forward_post("/api/video/close")


@app.post("/api/alert/ack")
async def api_alert_ack() -> Response:
    return await _forward_post("/api/alert/ack")


@app.post("/api/alert/reset")
async def api_alert_reset() -> Response:
    return await _forward_post("/api/alert/reset")


def _build_upstream_ws_url(ws: WebSocket, upstream_path: str) -> str:
    base = _pi_ws_base()
    path = upstream_path.lstrip("/")
    url = f"{base}/{path}"
    if ws.url.query:
        url = f"{url}?{ws.url.query}"
    return url


async def _safe_close_ws(ws: WebSocket, code: int = 1011, reason: str = "") -> None:
    try:
        if ws.client_state != WebSocketState.DISCONNECTED:
            await ws.close(code=code, reason=reason)
    except Exception:
        pass


def _websocket_connect_kwargs(headers: dict[str, str]) -> dict:
    kwargs = {
        "ping_interval": 20,
        "ping_timeout": 20,
        "open_timeout": 20,
        "close_timeout": 10,
        "max_size": None,
    }
    if headers:
        kwargs["extra_headers"] = headers
    return kwargs


async def _ws_passthrough(ws: WebSocket, upstream_path: str) -> None:
    upstream_url = _build_upstream_ws_url(ws, upstream_path)
    headers = _auth_headers()

    try:
        async with websockets.connect(upstream_url, **_websocket_connect_kwargs(headers)) as upstream:
            await ws.accept()

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            break
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                finally:
                    try:
                        await upstream.close()
                    except Exception:
                        pass

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await ws.send_bytes(message)
                        else:
                            await ws.send_text(message)
                finally:
                    await _safe_close_ws(ws, code=1011, reason="Upstream websocket closed")

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())

            done, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)

            for task in pending:
                task.cancel()

            for task in done:
                await task

    except Exception:
        logger.exception("Failed to establish upstream websocket")
        await _safe_close_ws(ws, code=1011, reason="Upstream connection failed")


@app.websocket("/ws/sensors")
async def ws_sensors(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/sensors")


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/audio")


@app.post("/api/events")
async def api_events(payload: EventIn, request: Request, db: Session = Depends(get_db)):
    _require_rpi_event_token(request.headers.get("x-rpi-token"))

    allowed_events = {"intruded", "stress_buzz", "swarming"}
    allowed_kinds = {"audio", "video"}

    event = payload.event.strip().lower()
    kind = payload.kind.strip().lower()

    if event not in allowed_events:
        raise HTTPException(status_code=400, detail="Unsupported event")
    if kind not in allowed_kinds:
        raise HTTPException(status_code=400, detail="Unsupported kind")

    try:
        row = create_event(db, payload)
        return {"ok": True, "id": row.id}
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to insert event: {type(exc).__name__}")


@app.get("/api/events")
async def get_events(
    limit: int = Query(default=50, ge=1, le=500),
    kind: Optional[str] = Query(default=None),
    event: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    rows = crud_list_events(db, limit=limit, kind=kind, event=event)

    return {
        "ok": True,
        "items": [
            {
                "id": row.id,
                "device_id": row.device_id,
                "kind": row.kind,
                "event": row.event,
                "conf": row.conf,
                "payload": json.loads(row.payload_json) if row.payload_json else None,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ],
    }
