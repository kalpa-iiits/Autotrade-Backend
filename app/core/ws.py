"""Shared WebSocket loop used by every app's endpoint: auth, parsing, replies, errors."""

from typing import Any, Awaitable, Callable

from fastapi import WebSocket, WebSocketDisconnect, status
from pydantic import TypeAdapter, ValidationError

from app.accounts.registry import AccountRuntime
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
    runtime: AccountRuntime | None,
    connections: Callable[[AccountRuntime], ConnectionManager],
    on_disconnect: Callable[[WebSocket], Awaitable[None]] | None = None,
) -> None:
    """One broker account's socket. Only the account's owner (session or API token) gets in;
    anyone else, and unknown accounts, are refused alike.

    Replies go through the account's connection manager like pushed messages, so a client sees
    them in the order they happened (e.g. an order's update before the ack that placed it)."""
    user = await websocket_user(ws)
    if user is None:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="Not logged in")
        return
    if runtime is None or runtime.user_id != user.id:
        await ws.close(code=status.WS_1008_POLICY_VIOLATION, reason="No such broker account")
        return

    manager = connections(runtime)
    await ws.accept()
    manager.add(ws)
    send = manager.send
    send(ws, {"type": "welcome", "account_id": runtime.id, "broker": runtime.broker})
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
        manager.remove(ws)
