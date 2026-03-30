# proxy/main.py
from __future__ import annotations

import asyncio
import os
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
import websockets
from fastapi import FastAPI, Request, Response, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.websockets import WebSocketDisconnect

"""
Memory-only Render Proxy (Free plan friendly)

- Stores current PI_HTTP_BASE in memory (no persistent disk).
- Set it via POST /admin/set-pi (secured by ADMIN_TOKEN).
- Proxies:
  GET /api/latest
  GET /video.mjpg
  WS  /ws/audio

Security:
- If PI_API_TOKEN is set, the proxy adds:
    Authorization: Bearer <PI_API_TOKEN>
  to upstream requests (HTTP + WS).

Render stability:
- Adds GET / and GET /favicon.ico to avoid health-check 405 loops.
"""

ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "").strip()
DEFAULT_PI_HTTP_BASE = os.getenv("PI_HTTP_BASE", "").strip().rstrip("/")
PI_API_TOKEN = os.getenv("PI_API_TOKEN", "").strip()

PI_HTTP_BASE_RUNTIME = DEFAULT_PI_HTTP_BASE

HTTP_TIMEOUT_S = float(os.getenv("HTTP_TIMEOUT_S", "20"))
HTTP_CONNECT_TIMEOUT_S = float(os.getenv("HTTP_CONNECT_TIMEOUT_S", "5"))
WS_CONNECT_TIMEOUT_S = float(os.getenv("WS_CONNECT_TIMEOUT_S", "20"))

app = FastAPI(title="Hive Proxy (Render → RPi)")

_http: httpx.AsyncClient | None = None


@app.on_event("startup")
async def _startup() -> None:
    global _http
    _http = httpx.AsyncClient(
        timeout=httpx.Timeout(
            HTTP_TIMEOUT_S,
            connect=HTTP_CONNECT_TIMEOUT_S,
        )
    )


@app.on_event("shutdown")
async def _shutdown() -> None:
    global _http
    if _http:
        await _http.aclose()
        _http = None


def _apply_cors(resp: Response, request: Request) -> Response:
    origin = request.headers.get("origin") or "*"
    acrh = request.headers.get("access-control-request-headers") or "Content-Type, x-admin-token, Authorization"
    resp.headers["Access-Control-Allow-Origin"] = origin
    resp.headers["Vary"] = "Origin"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = acrh
    return resp


@app.middleware("http")
async def always_cors(request: Request, call_next):
    try:
        resp = await call_next(request)
    except Exception as e:
        resp = JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    return _apply_cors(resp, request)


@app.options("/{path:path}")
async def preflight(path: str, request: Request):
    return _apply_cors(Response(status_code=204), request)


def _validate_url(new_base: str) -> None:
    u = urlparse(new_base)
    if not u.scheme or not u.netloc:
        raise ValueError("Invalid URL")
    if any(ch.isspace() for ch in u.netloc):
        raise ValueError("Invalid host (whitespace)")


def _pi_http_base() -> str:
    base = (PI_HTTP_BASE_RUNTIME or "").strip().rstrip("/")
    if not base:
        raise RuntimeError("PI_HTTP_BASE not set. Call /admin/set-pi or set env PI_HTTP_BASE.")
    if not base.startswith(("http://", "https://")):
        raise RuntimeError("PI_HTTP_BASE must start with http:// or https://")
    return base


def _pi_ws_base() -> str:
    base = _pi_http_base()
    if base.startswith("https://"):
        return "wss://" + base.removeprefix("https://")
    return "ws://" + base.removeprefix("http://")


def _upstream_headers_http() -> dict[str, str]:
    if not PI_API_TOKEN:
        return {}
    return {"Authorization": f"Bearer {PI_API_TOKEN}"}


def _upstream_headers_ws() -> list[tuple[str, str]] | None:
    if not PI_API_TOKEN:
        return None
    return [("Authorization", f"Bearer {PI_API_TOKEN}")]


@app.get("/")
async def root():
    return {"ok": True, "service": "proxy", "health": "/health"}


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/health")
async def health() -> dict:
    return {
        "ok": True,
        "pi_http_base": PI_HTTP_BASE_RUNTIME or None,
        "has_admin_token": bool(ADMIN_TOKEN),
        "has_pi_api_token": bool(PI_API_TOKEN),
    }


@app.get("/admin/get-pi")
async def admin_get_pi() -> dict:
    return {"ok": True, "pi_http_base": PI_HTTP_BASE_RUNTIME or None}


@app.post("/admin/set-pi")
async def admin_set_pi(payload: dict, request: Request) -> Response:
    global PI_HTTP_BASE_RUNTIME

    if not ADMIN_TOKEN:
        return JSONResponse(
            {"ok": False, "error": "ADMIN_TOKEN not configured in Render env"},
            status_code=500,
        )

    token = request.headers.get("x-admin-token", "")
    if token != ADMIN_TOKEN:
        return JSONResponse(
            {"ok": False, "error": "Unauthorized: set header x-admin-token"},
            status_code=401,
        )

    raw = str(payload.get("pi_http_base", "")).strip()
    new_base = raw.rstrip("/")

    if not new_base.startswith(("http://", "https://")):
        return JSONResponse(
            {"ok": False, "error": "pi_http_base must start with http:// or https://"},
            status_code=400,
        )

    try:
        _validate_url(new_base)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    PI_HTTP_BASE_RUNTIME = new_base
    print("[PROXY] PI_HTTP_BASE_RUNTIME set to:", PI_HTTP_BASE_RUNTIME)
    return JSONResponse({"ok": True, "pi_http_base": PI_HTTP_BASE_RUNTIME})


@app.get("/api/latest")
async def api_latest() -> Response:
    if _http is None:
        return JSONResponse({"ok": False, "error": "http client not ready"}, status_code=503)

    try:
        url = f"{_pi_http_base()}/api/latest"
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    try:
        r = await _http.get(url, headers=_upstream_headers_http())
        return Response(
            content=r.content,
            status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/json"),
        )
    except Exception as e:
        print("[PROXY] /api/latest upstream error:", repr(e))
        return JSONResponse({"ok": False, "error": f"upstream unreachable: {e}"}, status_code=502)


async def _stream_upstream(url: str) -> AsyncIterator[bytes]:
    timeout = httpx.Timeout(connect=HTTP_CONNECT_TIMEOUT_S, read=None, write=10, pool=10)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("GET", url, headers=_upstream_headers_http()) as r:
            r.raise_for_status()
            async for chunk in r.aiter_bytes():
                if chunk:
                    yield chunk


@app.get("/video.mjpg")
async def video_mjpg() -> Response:
    try:
        url = f"{_pi_http_base()}/video.mjpg"
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    try:
        return StreamingResponse(
            _stream_upstream(url),
            media_type="multipart/x-mixed-replace; boundary=frame",
            headers={
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )
    except Exception as e:
        print("[PROXY] /video.mjpg upstream error:", repr(e))
        return JSONResponse({"ok": False, "error": f"upstream unreachable: {e}"}, status_code=502)


async def _connect_upstream_ws(url: str, headers: list[tuple[str, str]] | None):
    # websockets renamed extra_headers -> additional_headers in some versions
    try:
        return websockets.connect(
            url,
            extra_headers=headers,
            open_timeout=WS_CONNECT_TIMEOUT_S,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        )
    except TypeError:
        return websockets.connect(
            url,
            additional_headers=headers,
            open_timeout=WS_CONNECT_TIMEOUT_S,
            ping_interval=20,
            ping_timeout=20,
            max_size=None,
        )


@app.websocket("/ws/audio")
async def ws_audio(ws: WebSocket) -> None:
    # 1. Tanggapin ang connection mula sa Browser
    await ws.accept()

    try:
        upstream_url = f"{_pi_ws_base()}/ws/audio"
    except Exception as e:
        await ws.close(code=1011, reason="PI_HTTP_BASE not set")
        return

    print(f"[PROXY WS] Attempting upstream connect to: {upstream_url}")

    try:
        # 2. Dagdagan ang timeout at i-disable ang compression para sa bilis
        async with await _connect_upstream_ws(upstream_url, _upstream_headers_ws()) as up:
            print("[PROXY WS] Connection to RPi SUCCESS")

            async def client_to_upstream() -> None:
                try:
                    while True:
                        msg = await ws.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        
                        # KEEP-ALIVE: Kung nakatanggap ng ping, sumagot ng pong
                        # at ipasa rin sa upstream.
                        if msg.get("text") == "ping":
                            await ws.send_text("pong")
                            await up.send("ping")
                            continue

                        if msg.get("text") is not None:
                            await up.send(msg["text"])
                        elif msg.get("bytes") is not None:
                            await up.send(msg["bytes"])
                except Exception:
                    pass

            async def upstream_to_client() -> None:
                try:
                    while True:
                        data = await up.recv()
                        if isinstance(data, (bytes, bytearray)):
                            await ws.send_bytes(bytes(data))
                        else:
                            await ws.send_text(str(data))
                except Exception:
                    pass

            t1 = asyncio.create_task(client_to_upstream())
            t2 = asyncio.create_task(upstream_to_client())
            
            # Hintayin ang kahit alin sa dalawa na matapos
            done, pending = await asyncio.wait(
                {t1, t2}, return_when=asyncio.FIRST_COMPLETED
            )
            for p in pending:
                p.cancel()

    except Exception as e:
        # DITO NATIN MAKIKITA KUNG BAKIT AYAW
        print(f"[PROXY WS] CONNECTION TO RPI FAILED: {repr(e)}")
        try:
            await ws.close(code=1011, reason=f"RPi Unreachable: {str(e)[:50]}")
        except Exception:
            pass
