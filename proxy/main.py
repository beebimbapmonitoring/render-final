from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from starlette.websockets import WebSocketDisconnect

# Persistent disk mount on Render
PERSIST_DIR = Path(os.getenv("PERSIST_DIR", "/var/data"))
TARGET_FILE = PERSIST_DIR / "pi_http_base.txt"

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEFAULT_PI_HTTP_BASE = os.getenv("PI_HTTP_BASE", "").strip().rstrip("/")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _load_target() -> str:
    if TARGET_FILE.exists():
        v = TARGET_FILE.read_text(encoding="utf-8").strip().rstrip("/")
        if v:
            return v
    return DEFAULT_PI_HTTP_BASE.rstrip("/")


def _save_target(v: str) -> None:
    PERSIST_DIR.mkdir(parents=True, exist_ok=True)
    TARGET_FILE.write_text(v.strip().rstrip("/") + "\n", encoding="utf-8")


def _pi_http_base() -> str:
    base = _load_target()
    if not base:
        raise RuntimeError("PI_HTTP_BASE not set. Set env PI_HTTP_BASE or call /admin/set-pi.")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError("PI_HTTP_BASE must start with http:// or https://")
    return base.rstrip("/")


def _pi_ws_base() -> str:
    base = _pi_http_base()
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://")
    return "ws://" + base.removeprefix("http://")


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "pi_http_base": _load_target() or None,
        "has_admin_token": bool(ADMIN_TOKEN),
        "target_file": str(TARGET_FILE),
    }


@app.post("/admin/set-pi")
async def admin_set_pi(payload: dict, request: Request) -> Response:
    if not ADMIN_TOKEN:
        return Response("ADMIN_TOKEN not configured", status_code=500)

    token = request.headers.get("x-admin-token", "")
    if token != ADMIN_TOKEN:
        return Response("Unauthorized", status_code=401)

    new_base = str(payload.get("pi_http_base", "")).strip().rstrip("/")
    if not new_base.startswith(("http://", "https://")):
        return Response("pi_http_base must start with http:// or https://", status_code=400)

    _save_target(new_base)
    return Response(
        content=f'{{"ok":true,"pi_http_base":"{new_base}"}}',
        media_type="application/json",
    )


@app.get("/api/latest")
async def api_latest() -> Response:
    url = f"{_pi_http_base()}/api/latest"
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url)
    return Response(
        content=r.content,
        status_code=r.status_code,
        media_type=r.headers.get("content-type", "application/json"),
    )


async def _stream_upstream(url: str) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(connect=10, read=None, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url) as r:
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


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    await ws.accept()
    upstream_url = f"{_pi_ws_base()}/ws/audio"

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.ws_connect(upstream_url) as up:

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
