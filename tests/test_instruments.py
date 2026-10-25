from app.brokers.base import Instrument
from app.brokers.fyers import parse_nse_instruments, parse_symbol_specs
from app.strategies.instruments import search_instruments
from tests.conftest import AUTH


FYERS_ROWS = """101,INFOSYS LIMITED,0,1,0.05,INE009A01021,x,2026-10-23,,NSE:INFY-EQ,10,10,1594,INFY
102,NIFTY50-INDEX,10,0,0.05,,x,2026-10-23,,NSE:NIFTY50-INDEX,10,10,26000,NIFTY
103,NIFTY ETF,9,1,0.05,INF,x,2026-10-23,,NSE:NIFTYBEES-EQ,10,10,10576,NIFTYBEES
"""


def test_fyers_nse_master_keeps_stocks_and_indexes_with_exact_symbols():
    got = parse_nse_instruments(FYERS_ROWS)
    assert [(item.symbol, item.ticker, item.kind) for item in got] == [
        ("NSE:NIFTY50-INDEX", "NIFTY", "index"),
        ("NSE:INFY-EQ", "INFY", "stock"),
    ]


def test_fyers_master_tick_and_lot_sizes():
    fo = "201,NIFTY 23 Nov 26 FUT,11,65,0.1,,x,2026-10-23,1795428600,NSE:NIFTY26NOVFUT,10,11,61471,NIFTY\n"
    assert parse_symbol_specs(FYERS_ROWS + fo) == {
        "NSE:INFY-EQ": (0.05, 1), "NSE:NIFTY50-INDEX": (0.05, 1), "NSE:NIFTYBEES-EQ": (0.05, 1),
        "NSE:NIFTY26NOVFUT": (0.1, 65),
    }


def test_instrument_search_ranks_ticker_before_company_name():
    items = [
        Instrument("NSE:ABC-EQ", "ABC", "ALPHA BETA COMPANY", "stock"),
        Instrument("NSE:ALPHA-EQ", "ALPHA", "ALPHA LIMITED", "stock"),
    ]
    assert [item.ticker for item in search_instruments(items, "alp", 10)] == ["ALPHA", "ABC"]


def test_instrument_search_endpoint_uses_account_broker(client, monkeypatch):
    broker = client.app.state.accounts.get(1).service.broker

    async def instruments(_exchange="NSE"):
        return [
            Instrument("NSE:NIFTY50-INDEX", "NIFTY", "NIFTY50-INDEX", "index"),
            Instrument("NSE:INFY-EQ", "INFY", "INFOSYS LIMITED", "stock"),
        ]

    monkeypatch.setattr(broker, "get_instruments", instruments)
    response = client.get("/api/strategies/instruments/search?account_id=1&q=inf", headers=AUTH)
    assert response.status_code == 200
    assert response.json() == [{
        "symbol": "NSE:INFY-EQ", "ticker": "INFY", "name": "INFOSYS LIMITED", "kind": "stock",
    }]


def test_instrument_search_requires_owned_account(client):
    response = client.get("/api/strategies/instruments/search?account_id=999&q=inf", headers=AUTH)
    assert response.status_code == 404
