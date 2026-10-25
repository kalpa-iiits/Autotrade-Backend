import asyncio
import logging

from fastapi import WebSocket, status

log = logging.getLogger(__name__)


class ConnectionManager:
    """Tracks open WebSockets for one app and the topics (symbols) each one follows.

    Every message to a socket (replies and pushes alike, so they keep their order) goes into
    that socket's own queue, written out by its own task. Broadcasting never waits on a
    client: one that falls `max_queue` messages behind is disconnected instead of slowing
    the broker feed for everyone.
    """

    def __init__(self, max_queue: int = 1000) -> None:
        self.max_queue = max_queue
        self.subscriptions: dict[WebSocket, set[str]] = {}
        self._outboxes: dict[WebSocket, asyncio.Queue] = {}
        self._writers: dict[WebSocket, asyncio.Task] = {}
        self.evicted = 0  # clients dropped for being too slow

    def add(self, ws: WebSocket) -> None:
        self.subscriptions[ws] = set()
        self._outboxes[ws] = queue = asyncio.Queue(self.max_queue)
        self._writers[ws] = asyncio.create_task(self._write(ws, queue))

    def remove(self, ws: WebSocket) -> set[str]:
        """Forget a socket; returns the topics it was following."""
        self._outboxes.pop(ws, None)
        if (writer := self._writers.pop(ws, None)) is not None and writer is not asyncio.current_task():
            writer.cancel()
        return self.subscriptions.pop(ws, set())

    def watched(self) -> set[str]:
        """Every topic at least one open socket follows."""
        return set().union(*self.subscriptions.values())

    def __len__(self) -> int:
        return len(self.subscriptions)

    async def _write(self, ws: WebSocket, queue: asyncio.Queue) -> None:
        while True:
            message = await queue.get()
            try:
                await ws.send_json(message)
            except Exception:
                # Client went away mid-send; its own handler will clean up.
                self.remove(ws)
                return

    def send(self, ws: WebSocket, message: dict) -> None:
        queue = self._outboxes.get(ws)
        if queue is None:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            self.evicted += 1
            log.warning("WebSocket client %s is %d messages behind; disconnecting it",
                        ws.client, self.max_queue)
            self.remove(ws)
            asyncio.create_task(self._close(ws))

    @staticmethod
    async def _close(ws: WebSocket) -> None:
        try:
            await ws.close(code=status.WS_1013_TRY_AGAIN_LATER, reason="Too slow; reconnect")
        except Exception:
            pass

    async def broadcast(self, message: dict) -> None:
        for ws in list(self.subscriptions):
            self.send(ws, message)

    async def publish(self, topic: str, message: dict) -> None:
        for ws, topics in list(self.subscriptions.items()):
            if topic in topics:
                self.send(ws, message)
