# proxy/main.py
from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from typing import AsyncIterator, Optional
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, Integer, String, Text, create_engine, desc
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker
from starlette.websockets import WebSocketDisconnect, WebSocketState

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("render-proxy")

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEFAULT_PI_HTTP_BASE = os.getenv("PI_HTTP_BASE", "").strip().rstrip("/")
PI_API_TOKEN = os.getenv("PI_API_TOKEN", "").strip()
RPI_EVENT_TOKEN = os.getenv("RPI_EVENT_TOKEN", "").strip()
DB_URL = os.getenv("DB_URL", "").strip()

PI_HTTP_BASE_RUNTIME = DEFAULT_PI_HTTP_BASE

app = FastAPI(title="Hive Render Proxy")


class Base(DeclarativeBase):
    pass


class EventLog(Base):
    __tablename__ = "event_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    device_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True, default="rpi")
    kind: Mapped[str] = mapped_column(String(16), nullable=False, index=True)  # audio|video
    event: Mapped[str] = mapped_column(String(64), nullable=False, index=True)  # intruded|stress_buzz|swarming
    conf: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    payload_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False, default=datetime.utcnow, index=True)


class EventIn(BaseModel):
    device_id: str = Field(default="rpi", max_length=64)
    kind: str = Field(..., max_length=16)
    event: str = Field(..., max_length=64)
    conf: float = 0.0
    payload: Optional[dict] = None


_ENGINE = None
SessionLocal = None


def _init_db() -> None:
    global _ENGINE, SessionLocal

    if not DB_URL:
        logger.warning("DB_URL not configured. Event logging endpoints will not work.")
        return

    _ENGINE = create_engine(DB_URL, pool_pre_ping=True)
    SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=_ENGINE)
    Base.metadata.create_all(bind=_ENGINE)
    logger.info("Database initialized.")


def _require_db() -> None:
    if SessionLocal is None:
        raise HTTPException(status_code=500, detail="Database is not configured. Set DB_URL on Render.")


def _db_session() -> Session:
    _require_db()
    return SessionLocal()


app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def startup_event() -> None:
    _init_db()


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
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, x-admin-token, x-rpi-token, Authorization"
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


def _require_rpi_event_token(x_rpi_token: str | None) -> None:
    if not RPI_EVENT_TOKEN:
        return
    if x_rpi_token != RPI_EVENT_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")


@app.get("/health")
async def health() -> dict:
    db_ok = False
    if SessionLocal is not None:
        try:
            db = _db_session()
            try:
                db.execute(EventLog.__table__.select().limit(1))
                db_ok = True
            finally:
                db.close()
        except Exception:
            db_ok = False

    return {
        "ok": True,
        "pi_http_base": PI_HTTP_BASE_RUNTIME or None,
        "has_admin_token": bool(ADMIN_TOKEN),
        "has_pi_api_token": bool(PI_API_TOKEN),
        "has_rpi_event_token": bool(RPI_EVENT_TOKEN),
        "has_db_url": bool(DB_URL),
        "db_ok": db_ok,
    }


@app.get("/debug/upstream")
async def debug_upstream() -> dict:
    base = _pi_http_base()
    url = f"{base}/api/latest"
    try:
        async with httpx.AsyncClient(timeout=12, follow_redirects=True) as client:
            r = await client.get(url, headers=_auth_headers())
        return {
            "ok": True,
            "url": url,
            "status_code": r.status_code,
            "content_type": r.headers.get("content-type", ""),
            "preview": r.text[:300],
        }
    except Exception as e:
        logger.exception("Upstream HTTP debug failed")
        return {
            "ok": False,
            "url": url,
            "error_type": type(e).__name__,
            "error": str(e),
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


async def _forward_get(path: str, timeout_s: float = 12.0) -> Response:
    url = f"{_pi_http_base()}/{path.lstrip('/')}"
    logger.info("Forwarding GET to %s", url)

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
    timeout = httpx.Timeout(connect=12, read=None, write=12, pool=12)
    logger.info("Streaming upstream from %s", url)

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

    logger.info("Connecting upstream websocket: %s", upstream_url)

    try:
        connect_kwargs = _websocket_connect_kwargs(headers)

        async with websockets.connect(upstream_url, **connect_kwargs) as upstream:
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

    except TypeError as e:
        logger.exception("websockets.connect argument mismatch")
        await ws.accept()
        await ws.send_text(f"WS proxy config error: {type(e).__name__}: {e}")
        await _safe_close_ws(ws, code=1011, reason="WS proxy config error")
    except Exception as e:
        logger.exception("Failed to establish upstream websocket")
        await _safe_close_ws(ws, code=1011, reason=f"Upstream connection failed: {type(e).__name__}")


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/audio")


@app.websocket("/ws/sensors")
async def ws_sensors(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/sensors")


@app.post("/api/events")
async def api_events(payload: EventIn, request: Request):
    _require_db()
    _require_rpi_event_token(request.headers.get("x-rpi-token"))

    allowed_events = {"intruded", "stress_buzz", "swarming"}
    allowed_kinds = {"audio", "video"}

    event = payload.event.strip().lower()
    kind = payload.kind.strip().lower()

    if event not in allowed_events:
        raise HTTPException(status_code=400, detail="Unsupported event")
    if kind not in allowed_kinds:
        raise HTTPException(status_code=400, detail="Unsupported kind")

    db = _db_session()
    try:
        row = EventLog(
            device_id=payload.device_id.strip() or "rpi",
            kind=kind,
            event=event,
            conf=float(payload.conf or 0.0),
            payload_json=json.dumps(payload.payload) if payload.payload is not None else None,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return {"ok": True, "id": row.id}
    except Exception as e:
        db.rollback()
        logger.exception("Failed to insert event")
        raise HTTPException(status_code=500, detail=f"Failed to insert event: {type(e).__name__}")
    finally:
        db.close()


@app.get("/api/events")
async def list_events(
    limit: int = Query(default=50, ge=1, le=500),
    kind: Optional[str] = Query(default=None),
    event: Optional[str] = Query(default=None),
):
    _require_db()

    db = _db_session()
    try:
        q = db.query(EventLog)

        if kind:
            q = q.filter(EventLog.kind == kind.strip().lower())
        if event:
            q = q.filter(EventLog.event == event.strip().lower())

        rows = q.order_by(desc(EventLog.created_at), desc(EventLog.id)).limit(limit).all()

        return {
            "ok": True,
            "items": [
                {
                    "id": r.id,
                    "device_id": r.device_id,
                    "kind": r.kind,
                    "event": r.event,
                    "conf": r.conf,
                    "payload": json.loads(r.payload_json) if r.payload_json else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    finally:
        db.close()
