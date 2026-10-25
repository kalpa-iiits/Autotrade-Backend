from fastapi import WebSocket


class ConnectionManager:
    """Tracks open WebSockets for one app and the topics (symbols) each one follows."""

    def __init__(self) -> None:
        self.subscriptions: dict[WebSocket, set[str]] = {}

    def add(self, ws: WebSocket) -> None:
        self.subscriptions[ws] = set()

    def remove(self, ws: WebSocket) -> set[str]:
        """Forget a socket; returns the topics it was following."""
        return self.subscriptions.pop(ws, set())

    def watched(self) -> set[str]:
        """Every topic at least one open socket follows."""
        return set().union(*self.subscriptions.values())

    async def _send(self, ws: WebSocket, message: dict) -> None:
        try:
            await ws.send_json(message)
        except Exception:
            # Client went away mid-send; its own handler will clean up.
            self.remove(ws)

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.subscriptions):
            await self._send(ws, message)

    async def publish(self, topic: str, message: dict) -> None:
        for ws, topics in list(self.subscriptions.items()):
            if topic in topics:
                await self._send(ws, message)
