from tests.conftest import TOKEN, recv_until, sign_in

AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_orders_are_recorded_and_updated(client):
    with client.websocket_connect(f"/api/accounts/1/ws/orders?token={TOKEN}") as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 3})
        filled = recv_until(ws, "ack")["order"]
        ws.send_json({"action": "place_order", "symbol": "NSE:TCS-EQ", "side": "BUY", "quantity": 1,
                      "order_type": "LIMIT", "limit_price": 1.0})
        limit = recv_until(ws, "ack")["order"]
        ws.send_json({"action": "cancel_order", "order_id": limit["id"]})
        recv_until(ws, "ack")

    history = client.get("/api/accounts/1/orders/history", headers=AUTH).json()
    by_id = {o["id"]: o for o in history}
    assert len(history) == 2  # the cancel updated the limit order in place
    assert by_id[filled["id"]]["status"] == "FILLED" and by_id[filled["id"]]["broker"] == "paper"
    assert by_id[limit["id"]]["status"] == "CANCELLED"
    assert history[0]["id"] == limit["id"]  # newest update first

    only_tcs = client.get("/api/accounts/1/orders/history?symbol=NSE:TCS-EQ", headers=AUTH).json()
    assert [o["id"] for o in only_tcs] == [limit["id"]]
    assert client.get("/api/accounts/1/orders/history?limit=1", headers=AUTH).json()[0]["id"] == limit["id"]


def test_history_survives_restart(client):
    with client.websocket_connect(f"/api/accounts/1/ws/orders?token={TOKEN}") as ws:
        ws.receive_json()
        ws.send_json({"action": "place_order", "symbol": "NSE:SBIN-EQ", "side": "BUY", "quantity": 1})
        order_id = recv_until(ws, "ack")["order"]["id"]
    client.__exit__(None, None, None)

    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as fresh:
        sign_in(fresh)
        assert [o["id"] for o in fresh.get("/api/accounts/1/orders/history").json()] == [order_id]


def test_history_needs_login(client):
    assert client.get("/api/accounts/1/orders/history").status_code == 401
