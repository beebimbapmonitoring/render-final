# /proxy/main.py
from __future__ import annotations

import os
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect

"""
Memory-only Render Proxy (Free plan friendly)

- Stores current PI_HTTP_BASE in memory (no persistent disk).
- Set it via POST /admin/set-pi (secured by ADMIN_TOKEN).
- Proxies:
  GET /api/latest        -> Pi /api/latest (should include sensors)
  GET /api/sensors       -> Pi /api/sensors (optional)
  GET /video.mjpg        -> Pi /video.mjpg
  WS  /ws/audio          -> Pi /ws/audio
  WS  /ws/sensors        -> Pi /ws/sensors (optional)

Security:
- If PI_API_TOKEN is set, proxy adds: Authorization: Bearer <token> to Pi requests.
- Put PI_HTTP_BASE = https://jonard.tail630aa1.ts.net in Render env for Funnel.
"""

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
    """
    Keeps CORS headers even when errors happen, so browser can read error responses.
    """
    try:
        resp = await call_next(request)
    except Exception as e:
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
    return JSONResponse({"ok": True, "pi_http_base": PI_HTTP_BASE_RUNTIME})


async def _forward_get(path: str, timeout_s: float = 8.0) -> Response:
    url = f"{_pi_http_base()}/{path.lstrip('/')}"
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        r = await client.get(url, headers=_auth_headers())
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


@app.get("/api/latest")
async def api_latest() -> Response:
    # RPi should include sensors in this payload for simplest frontend
    return await _forward_get("/api/latest")


@app.get("/api/sensors")
async def api_sensors() -> Response:
    # Optional: if you implement Pi /api/sensors
    return await _forward_get("/api/sensors")


async def _stream_upstream(url: str) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(connect=8, read=None, write=8, pool=8)
    async with httpx.AsyncClient(timeout=timeout) as client:
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


async def _ws_passthrough(ws: WebSocket, upstream_path: str) -> None:
    await ws.accept()
    upstream_url = f"{_pi_ws_base()}/{upstream_path.lstrip('/')}"
    headers = _auth_headers()

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.ws_connect(
                upstream_url,
                headers=headers,
                ping_interval=20,
                ping_timeout=20,
                timeout=20,
            ) as up:

                async def client_to_upstream() -> None:
                    try:
                        while True:
                            msg = await ws.receive()
                            if msg.get("type") == "websocket.disconnect":
                                break
                            if msg.get("text") is not None:
                                await up.send_text(msg["text"])
                            elif msg.get("bytes") is not None:
                                await up.send_bytes(msg["bytes"])
                    except WebSocketDisconnect:
                        pass
                    except Exception:
                        pass

                async def upstream_to_client() -> None:
                    try:
                        while True:
                            m = await up.receive()
                            if m.type == httpx.WSMsgType.TEXT:
                                await ws.send_text(m.data)
                            elif m.type == httpx.WSMsgType.BINARY:
                                await ws.send_bytes(m.data)
                            else:
                                break
                    except Exception:
                        pass

                import asyncio

                t1 = asyncio.create_task(client_to_upstream())
                t2 = asyncio.create_task(upstream_to_client())
                _, pending = await asyncio.wait({t1, t2}, return_when=asyncio.FIRST_COMPLETED)
                for p in pending:
                    p.cancel()

        except Exception:
            try:
                await ws.close()
            except Exception:
                pass


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await _ws_passthrough(ws, "/ws/audio")


@app.websocket("/ws/sensors")
async def ws_sensors(ws: WebSocket) -> None:
    # Optional: if you implement Pi /ws/sensors
    await _ws_passthrough(ws, "/ws/sensors")
