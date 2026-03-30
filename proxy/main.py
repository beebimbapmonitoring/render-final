from __future__ import annotations

import asyncio
import logging
import os
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect, WebSocketState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("render-proxy")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEFAULT_PI_HTTP_BASE = os.getenv("PI_HTTP_BASE", "").strip().rstrip("/")
PI_API_TOKEN = os.getenv("PI_API_TOKEN", "").strip()

PI_HTTP_BASE_RUNTIME = DEFAULT_PI_HTTP_BASE

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def always_cors(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception as e:
        logger.exception("Unhandled HTTP proxy error")
        resp = JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    origin = request.headers.get("origin", "*")
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-admin-token, Authorization"
    return resp


@app.options("/{path:path}")
async def preflight(path: str):
    return Response(status_code=204)


def _validate_url(new_base: str) -> None:
    u = urlparse(new_base)
    if not u.scheme or not u.netloc:
        raise ValueError("Invalid URL")
    if any(ch.isspace() for ch in u.netloc):
        raise ValueError("Invalid host (whitespace)")


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


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "pi_http_base": PI_HTTP_BASE_RUNTIME or None,
        "has_admin_token": bool(ADMIN_TOKEN),
        "has_pi_api_token": bool(PI_API_TOKEN),
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
    except ValueError as e:
        return Response(str(e), status_code=400)

    PI_HTTP_BASE_RUNTIME = new_base
    logger.info("Updated PI_HTTP_BASE_RUNTIME to %s", PI_HTTP_BASE_RUNTIME)
    return JSONResponse({"ok": True, "pi_http_base": PI_HTTP_BASE_RUNTIME})


async def _forward_get(path: str, timeout_s: float = 8.0) -> Response:
    url = f"{_pi_http_base()}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_s, follow_redirects=True) as client:
        r = await client.get(url, headers=_auth_headers())
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


@app.get("/api/latest")
async def api_latest() -> Response:
    return await _forward_get("/api/latest")


@app.get("/api/sensors")
async def api_sensors() -> Response:
    return await _forward_get("/api/sensors")


async def _stream_upstream(url: str) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(connect=8, read=None, write=8, pool=8)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        async with client.stream("GET", url, headers=_auth_headers()) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk


@app.get("/video.mjpg")
async def video_mjpg() -> StreamingResponse:
    url = f"{_pi_http_base()}/video.mjpg"
    return StreamingResponse(
        _stream_upstream(url),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


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


async def _ws_passthrough(ws: WebSocket, upstream_path: str) -> None:
    upstream_url = _build_upstream_ws_url(ws, upstream_path)
    headers = _auth_headers()

    logger.info("Connecting upstream websocket: %s", upstream_url)

    try:
        async with websockets.connect(
            upstream_url,
            additional_headers=headers if headers else None,
            ping_interval=20,
            ping_timeout=20,
            open_timeout=20,
            close_timeout=10,
            max_size=None,
        ) as upstream:
            await ws.accept()
            logger.info("WebSocket relay established: client <-> %s", upstream_url)

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        if msg["type"] == "websocket.disconnect":
                            logger.info("Client disconnected")
                            break
                        if msg.get("text") is not None:
                            await upstream.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await upstream.send(msg["bytes"])
                except WebSocketDisconnect:
                    logger.info("Client websocket disconnected")
                except Exception:
                    logger.exception("Error forwarding client -> upstream")
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
                except Exception:
                    logger.exception("Error forwarding upstream -> client")
                finally:
                    await _safe_close_ws(ws, code=1011, reason="Upstream websocket closed")

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())

            done, pending = await asyncio.wait(
                {t1, t2},
                return_when=asyncio.FIRST_COMPLETED,
            )

            for task in pending:
                task.cancel()

            for task in done:
                try:
                    await task
                except Exception:
                    logger.exception("Relay task failed")

    except Exception as e:
        logger.exception("Failed to establish upstream websocket")
        await _safe_close_ws(ws, code=1011, reason=f"Upstream connection failed: {type(e).__name__}")


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/audio")


@app.websocket("/ws/sensors")
async def ws_sensors(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/sensors")
