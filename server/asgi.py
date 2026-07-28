import asyncio
import json
import time
from contextlib import asynccontextmanager

import anyio
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.middleware.wsgi import WSGIMiddleware
from starlette.responses import JSONResponse
from starlette.routing import Mount, WebSocketRoute
from starlette.websockets import WebSocketDisconnect

from app import app as flask_app
from app import (
    DEVICE_SYNC_SECONDS,
    OUTBOUND_STALE_SECONDS,
    apply_outbound_sms_result,
    authenticate_mcp_token,
    claim_outbound_sms,
    db_connect,
    device_token_valid,
    text_value,
    upsert_device,
    utc_now,
)
from mcp_server import mcp


class MCPTokenAuthMiddleware:
    """Authenticate every MCP HTTP request with a managed full-access token."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        authorization = Headers(scope=scope).get("authorization", "")
        scheme, separator, raw_token = authorization.partition(" ")
        if (
            not separator
            or scheme.lower() != "bearer"
            or not raw_token.strip()
            or not await anyio.to_thread.run_sync(
                authenticate_mcp_token,
                raw_token.strip(),
            )
        ):
            response = JSONResponse(
                {"error": "invalid or expired MCP token"},
                status_code=401,
                headers={
                    "WWW-Authenticate": 'Bearer realm="sms-center-mcp"',
                    "Cache-Control": "no-store",
                },
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


mcp_http_app = mcp.streamable_http_app()


def websocket_client_ip(websocket):
    forwarded = Headers(scope=websocket.scope).get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()[:80]
    client = websocket.scope.get("client")
    return text_value(client[0] if client else "", 80)


def register_websocket_device(data, remote_ip):
    with db_connect() as db:
        return upsert_device(db, data, remote_ip=remote_ip)


def touch_websocket_device(device_id, remote_ip):
    with db_connect() as db:
        db.execute(
            """
            UPDATE devices
            SET last_seen=?, last_ip=?, status_message=''
            WHERE id=?
            """,
            (utc_now(), remote_ip, device_id),
        )


def claim_websocket_command(device_id):
    with db_connect() as db:
        db.execute("BEGIN IMMEDIATE")
        return claim_outbound_sms(db, device_id)


def save_websocket_result(device_id, data):
    payload = dict(data)
    payload["device_id"] = device_id
    with db_connect() as db:
        return apply_outbound_sms_result(db, payload)


async def device_websocket(websocket):
    """Low-traffic device channel for presence, commands and command results."""
    await websocket.accept()
    try:
        hello = await asyncio.wait_for(websocket.receive_json(), timeout=15)
    except WebSocketDisconnect:
        return
    except (asyncio.TimeoutError, json.JSONDecodeError):
        await websocket.close(code=1008)
        return
    if (
        not isinstance(hello, dict)
        or hello.get("type") != "hello"
        or not device_token_valid(hello.get("token"))
    ):
        await websocket.close(code=1008)
        return

    remote_ip = websocket_client_ip(websocket)
    try:
        device_id, _ = await anyio.to_thread.run_sync(
            register_websocket_device,
            hello,
            remote_ip,
        )
    except (TypeError, ValueError):
        await websocket.close(code=1008)
        return

    await websocket.send_json(
        {
            "type": "ready",
            "code": 0,
            "status_seconds": 21600,
            "fallback_sync_seconds": DEVICE_SYNC_SECONDS,
        }
    )
    in_flight = None
    in_flight_deadline = 0
    last_touch = time.monotonic()

    try:
        while True:
            if in_flight is not None and time.monotonic() >= in_flight_deadline:
                in_flight = None
            if in_flight is None:
                command = await anyio.to_thread.run_sync(
                    claim_websocket_command,
                    device_id,
                )
                if command:
                    in_flight = command["id"]
                    in_flight_deadline = time.monotonic() + OUTBOUND_STALE_SECONDS
                    await websocket.send_json({"type": "command", **command})

            if time.monotonic() - last_touch >= 60:
                await anyio.to_thread.run_sync(
                    touch_websocket_device,
                    device_id,
                    remote_ip,
                )
                last_touch = time.monotonic()

            try:
                message = await asyncio.wait_for(
                    websocket.receive_json(),
                    timeout=2,
                )
            except asyncio.TimeoutError:
                continue
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "error", "code": 400, "message": "invalid JSON"}
                )
                continue

            if not isinstance(message, dict):
                continue
            message_type = message.get("type")
            if message_type == "status":
                message["device_id"] = device_id
                await anyio.to_thread.run_sync(
                    register_websocket_device,
                    message,
                    remote_ip,
                )
                last_touch = time.monotonic()
            elif message_type == "result":
                try:
                    command_id = await anyio.to_thread.run_sync(
                        save_websocket_result,
                        device_id,
                        message,
                    )
                except ValueError:
                    await websocket.send_json(
                        {"type": "result_ack", "code": 400, "id": message.get("id")}
                    )
                    continue
                except LookupError:
                    await websocket.send_json(
                        {"type": "result_ack", "code": 404, "id": message.get("id")}
                    )
                    continue
                await websocket.send_json(
                    {"type": "result_ack", "code": 0, "id": command_id}
                )
                if command_id == in_flight:
                    in_flight = None
                    in_flight_deadline = 0
            else:
                await websocket.send_json(
                    {"type": "error", "code": 400, "message": "unknown message type"}
                )
    except WebSocketDisconnect:
        return


@asynccontextmanager
async def lifespan(_app):
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        WebSocketRoute("/api/device/ws", device_websocket),
        Mount("/mcp", app=MCPTokenAuthMiddleware(mcp_http_app)),
        Mount("/", app=WSGIMiddleware(flask_app)),
    ],
    lifespan=lifespan,
)
