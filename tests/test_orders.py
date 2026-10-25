import asyncio

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core.broker_service import BrokerService
from app.core.config import settings
from app.orders.schemas import Order, PlaceOrder, Side
from tests.conftest import TOKEN, recv_until, run

URL = f"/api/ws/orders?token={TOKEN}"


def test_rejects_bad_token(client):
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/ws/orders?token=wrong") as ws:
            ws.receive_json()


def test_market_order_fills_and_updates_position(client):
    with client.websocket_connect(URL) as ws:
        assert ws.receive_json()["type"] == "welcome"

        ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY",
                      "quantity": 10, "request_id": "r1"})
        update = recv_until(ws, "order_update")
        assert update["order"]["status"] == "FILLED"
        ack = recv_until(ws, "ack")
        assert ack["request_id"] == "r1"
        assert ack["order"]["status"] == "FILLED"

        ws.send_json({"action": "get_positions"})
        positions = recv_until(ws, "ack")["positions"]
        assert any(p["symbol"] == "NSE:SBIN-EQ" and p["quantity"] == 10 for p in positions)


def test_limit_order_can_be_cancelled(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:TCS-EQ", "side": "BUY",
                      "quantity": 1, "order_type": "LIMIT", "limit_price": 1.0})
        order = recv_until(ws, "ack")["order"]
        assert order["status"] == "OPEN"

        ws.send_json({"action": "cancel_order", "order_id": order["id"]})
        assert recv_until(ws, "ack")["order"]["status"] == "CANCELLED"


def test_invalid_message_returns_error(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:TCS-EQ", "side": "BUY",
                      "quantity": 1, "order_type": "LIMIT"})  # missing limit_price
        assert recv_until(ws, "error")["type"] == "error"
        # Ticker messages belong to the tickers app, not here.
        ws.send_json({"action": "subscribe", "symbols": ["NSE:TCS-EQ"]})
        assert recv_until(ws, "error")["type"] == "error"


def test_unknown_symbol_is_an_error(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NOPE", "side": "BUY", "quantity": 1})
        assert "Unknown symbol" in recv_until(ws, "error")["error"]


def test_ping(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "ping", "request_id": "p"})
        assert recv_until(ws, "pong")["request_id"] == "p"


def test_rest_reads_need_token(client):
    assert client.get("/api/orders").status_code == 401
    r = client.get("/api/account/funds", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json()["cash"] > 0


AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_same_client_order_id_places_once(client):
    body = {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 3, "client_order_id": "retry1"}
    first = client.post("/api/orders", json=body, headers=AUTH)
    again = client.post("/api/orders", json=body, headers=AUTH)  # e.g. a retry after a timeout
    assert first.status_code == again.status_code == 201
    assert first.json()["id"] == again.json()["id"]
    assert first.json()["client_order_id"] == "retry1"
    assert len(client.get("/api/orders", headers=AUTH).json()) == 1

    # The WebSocket shares the same guard; without an id every order gets its own.
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY",
                      "quantity": 3, "client_order_id": "retry1"})
        assert recv_until(ws, "ack")["order"]["id"] == first.json()["id"]
    other = client.post("/api/orders", json=body | {"client_order_id": None}, headers=AUTH).json()
    assert other["id"] != first.json()["id"] and other["client_order_id"]
    assert len(client.get("/api/orders", headers=AUTH).json()) == 2


def test_client_order_id_must_fit_broker_tags(client):
    for bad in ["has-dash", "x" * 21, ""]:
        body = {"symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1, "client_order_id": bad}
        assert client.post("/api/orders", json=body, headers=AUTH).status_code == 422


class FlakyBroker:
    """Fake broker: the first placement reaches the broker but the reply is lost."""

    def __init__(self):
        self.placed = []

    async def place_order(self, req):
        await asyncio.sleep(0.01)
        self.placed.append(Order(client_order_id=req.client_order_id, symbol=req.symbol,
                                 side=req.side, quantity=req.quantity, order_type=req.order_type))
        if len(self.placed) == 1:
            raise ConnectionError("read timed out")
        return self.placed[-1]

    async def get_orders(self):
        return self.placed


def test_retry_after_lost_reply_and_concurrent_retries_place_once(client):
    service = BrokerService(settings, token_loader=lambda: None)
    service.broker = broker = FlakyBroker()
    req = PlaceOrder(action="place_order", symbol="NSE:SBIN-EQ", side=Side.BUY, quantity=1,
                     client_order_id="lost1")

    async def scenario():
        # Two at once: one placement, both callers see its outcome.
        results = await asyncio.gather(service.place_order(req), service.place_order(req),
                                       return_exceptions=True)
        assert [type(r) for r in results] == [ConnectionError, ConnectionError]
        # The retry finds the order the lost reply was about, at the broker, instead of
        # placing a second one.
        found = await service.place_order(req)
        assert found.client_order_id == "lost1"
        return len(broker.placed)

    assert run(client, scenario) == 1
