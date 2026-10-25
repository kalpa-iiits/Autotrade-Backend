"""Shared WebSocket loop used by every app's endpoint: auth, parsing, replies, errors."""

from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect, status
from pydantic import TypeAdapter, ValidationError

from app.auth.dependencies import websocket_user
from app.brokers import OrderError
from app.core.connections import ConnectionManager
from app.core.schemas import Ping


# Takes a validated message, returns the extra fields for its "ack" reply.
Handler = Callable[[Any], Awaitable[dict]]


async def serve(
    ws: WebSocket,
    adapter: TypeAdapter,
    handle: Handler,
    connections: ConnectionManager,
    on_disconnect: Callable[[WebSocket], Awaitable[None]] | None = None,
) -> None:
    """Replies go through `connections` like pushed messages, so a client sees them in the
    order they happened (e.g. an order's update before the ack of the request that placed it)."""
    if await websocket_user(ws) is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not logged in")
        return

    await ws.accept()
    connections.add(ws)
    send = connections.send
    send(ws, {"type": "welcome", "broker": ws.app.state.brokers.name})
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = adapter.validate_json(raw)
            except ValidationError as e:
                send(ws, {"type": "error", "error": e.errors(include_url=False, include_context=False)})
                continue

            if isinstance(msg, Ping):
                send(ws, {"type": "pong", "request_id": msg.request_id})
                continue

            reply = {"type": "ack", "action": msg.action, "request_id": msg.request_id}
            try:
                reply.update(await handle(msg))
            except Exception as e:
                # OrderError is an expected refusal; anything else (e.g. broker API down)
                # is still reported instead of killing the socket.
                reply = {"type": "error", "action": msg.action, "request_id": msg.request_id,
                         "error": str(e) if isinstance(e, OrderError) else f"Broker error: {e}"}
            send(ws, reply)
    except WebSocketDisconnect:
        pass
    finally:
        if on_disconnect is not None:
            await on_disconnect(ws)
        connections.remove(ws)
