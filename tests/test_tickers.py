from tests.conftest import TOKEN, recv_until

URL = f"/api/ws/tickers?token={TOKEN}"


def test_subscribe_streams_only_requested_symbols(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "subscribe", "symbols": ["NSE:INFY-EQ"]})
        assert recv_until(ws, "ack")["symbols"] == ["NSE:INFY-EQ"]

        for _ in range(3):
            tick = recv_until(ws, "tick")
            assert tick["symbol"] == "NSE:INFY-EQ" and tick["ltp"] > 0

        ws.send_json({"action": "unsubscribe", "symbols": ["NSE:INFY-EQ"]})
        assert recv_until(ws, "ack")["symbols"] == []


def test_order_messages_are_rejected_here(client):
    with client.websocket_connect(URL) as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY",
                      "quantity": 1})
        assert recv_until(ws, "error")["type"] == "error"
