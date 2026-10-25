from collections import deque
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from app.core.config import settings
from app.main import app
from app.strategies import deployments
from app.strategies.base import Bar, BarBuilder, Strategy, Tick
from app.strategies.builtin.sma_crossover import SmaCrossover
from app.strategies.deployment import Deployment
from tests.conftest import ACCOUNT, TOKEN, add_user, run

AUTH = {"Authorization": f"Bearer {TOKEN}"}
SYM = "NSE:SBIN-EQ"
SCRIPT: deque = deque()  # commands the scripted strategy runs, one per tick


class Scripted(Strategy):
    """Test strategy: each tick runs the next command from SCRIPT."""
    name = "test_scripted"

    class Params(BaseModel):
        pass

    async def on_tick(self, tick: Tick) -> None:
        if not SCRIPT:
            return
        cmd, *args = SCRIPT.popleft()
        if cmd == "raise":
            raise RuntimeError("boom")
        await getattr(self.ctx, cmd)(*args)


def dep(key="test", **overrides):
    fields = {"key": key, "strategy": Scripted, "symbol": SYM, "quantity": 1,
              "max_position": 2, "max_orders_per_day": 5} | overrides
    return Deployment(**fields)


@pytest.fixture
def deploy(monkeypatch):
    """deploy(*deployments) -> a running app with exactly those deployments in code.
    Its paper broker sends no random ticks during the test."""
    monkeypatch.setattr(settings, "tick_interval", 3600)
    SCRIPT.clear()
    clients = []

    def start(*deps):
        monkeypatch.setattr(deployments, "DEPLOYMENTS", list(deps))
        if clients:
            clients[-1].__exit__(None, None, None)  # "restart the server"
        c = TestClient(app)
        c.__enter__()
        if not clients:
            add_user(c, "admin", token=TOKEN)  # with paper account ACCOUNT
        clients.append(c)
        for d in deps:
            run(c, ensure_instance, d)
        return c

    yield start
    if clients:
        clients[-1].__exit__(None, None, None)


async def ensure_instance(d: Deployment, account_id: int = ACCOUNT) -> None:
    """The deployment's first instance on the account, on its default symbol and timeframe,
    like a user adding it in the app (without the broker's symbol check)."""
    from app.core import db
    from app.strategies.models import StrategyInstance

    async with db.session() as s, s.begin():
        exists = await s.scalar(select(StrategyInstance.id).where(
            StrategyInstance.account_id == account_id, StrategyInstance.deployment_key == d.key))
        if exists is None:
            s.add(StrategyInstance(account_id=account_id, deployment_key=d.key, symbol=d.symbol,
                                   timeframe=d.timeframe, status="stopped", **d.settings()))


def by_key(client, key, include_removed=False):
    """The (first) instance of a deployment."""
    q = "?include_hidden=true" if include_removed else ""
    rows = client.get(f"/api/strategies{q}", headers=AUTH).json()["strategies"]
    return next((r for r in rows if r["deployment_key"] == key), None)


def start_strategy(client, key):
    inst = by_key(client, key)
    r = client.post(f"/api/strategies/{inst['id']}/start", headers=AUTH)
    assert r.status_code == 200, r.text
    return r.json()


def tick(client, price=100.0, symbol=SYM):
    """Feed a tick and wait until the strategies have handled it (they run on their own tasks)."""
    runner = client.app.state.strategy_runner
    run(client, runner.on_tick, ACCOUNT, symbol, price)
    run(client, runner.drain)


def signals(client, instance_id):
    return client.get(f"/api/strategies/{instance_id}/signals", headers=AUTH).json()


def instance(client, instance_id):
    return client.get(f"/api/strategies/{instance_id}", headers=AUTH).json()


# ---------- defined in code ----------

def test_deployments_come_from_code(deploy):
    c = deploy(dep("a"), dep("b", strategy=SmaCrossover, params={"fast": 5, "slow": 10}, quantity=3,
                             max_position=None))
    rows = c.get("/api/strategies", headers=AUTH).json()["strategies"]
    assert [(r["deployment_key"], r["symbol"], r["timeframe"]) for r in rows] == [
        ("a", SYM, "5m"), ("b", SYM, "5m")]
    b = rows[1]
    assert (b["strategy"], b["mode"], b["status"]) == ("sma_crossover", "signal", "stopped")
    assert (b["max_order_qty"], b["max_position"]) == (3, 3)  # default to quantity
    assert b["params"] == {"fast": 5, "slow": 10, "allow_short": False}


def test_logic_limits_and_mode_cannot_be_changed_in_the_app(deploy):
    c = deploy(dep("a"))
    inst = by_key(c, "a")
    # Only symbol/timeframe are accepted; anything else is ignored.
    r = c.patch(f"/api/strategies/{inst['id']}", json={"mode": "live", "quantity": 999,
                                                    "params": {"x": 1}}, headers=AUTH)
    assert r.status_code == 200
    assert (r.json()["mode"], r.json()["quantity"]) == ("signal", 1)
    # A brand-new strategy can't be defined from the app.
    assert c.post("/api/strategies", json={"deployment_key": "nope", "account_id": ACCOUNT, "symbol": SYM,
                                           "timeframe": "1m"}, headers=AUTH).status_code == 404


def test_invalid_deployments_fail_fast():
    with pytest.raises(ValueError, match="fast must be smaller"):
        Deployment(key="x", strategy=SmaCrossover, symbol=SYM, quantity=1, params={"fast": 30, "slow": 10})
    with pytest.raises(ValueError, match="lowercase"):
        Deployment(key="Bad Key", strategy=Scripted, symbol=SYM, quantity=1)
    with pytest.raises(ValueError, match="positive"):
        Deployment(key="x", strategy=Scripted, symbol=SYM, quantity=0)
    with pytest.raises(TypeError):
        Deployment(key="x", strategy=object, symbol=SYM, quantity=1)


def test_changed_settings_come_back_stopped(deploy):
    c = deploy(dep("a"), dep("b", symbol="NSE:TCS-EQ"))
    start_strategy(c, "a")
    start_strategy(c, "b")

    c = deploy(dep("a", mode="live"), dep("b", symbol="NSE:TCS-EQ"))  # only "a" changed
    a, b = by_key(c, "a"), by_key(c, "b")
    assert (a["status"], a["mode"]) == ("stopped", "live")
    assert "changed in code" in a["last_error"]
    assert b["status"] == "running"  # unchanged: resumes


def test_removed_and_readded(deploy):
    c = deploy(dep("a"), dep("b"))
    start_strategy(c, "b")
    c = deploy(dep("a"))
    assert by_key(c, "b") is None
    gone = by_key(c, "b", include_removed=True)
    assert gone["status"] == "removed"
    assert c.post(f"/api/strategies/{gone['id']}/start", headers=AUTH).status_code == 409

    c = deploy(dep("a"), dep("b"))
    assert by_key(c, "b")["status"] == "stopped"


def test_start_stop(deploy):
    c = deploy(dep("a"))
    started = start_strategy(c, "a")
    assert started["status"] == "running" and started["position"] == 0
    assert SYM in c.app.state.accounts.get(1).service.watched_symbols()  # kept subscribed for the strategy
    assert c.post(f"/api/strategies/{started['id']}/stop", headers=AUTH).json()["status"] == "stopped"


def test_requires_login(deploy):
    c = deploy(dep("a"))
    for method, path in [("get", "/api/strategies"), ("post", "/api/strategies/stop-all"), ("post", "/api/strategies/1/start")]:
        assert getattr(c, method)(path).status_code == 401


# ---------- signal mode (dry run) ----------

def test_signal_mode_tracks_virtual_position_and_limits(deploy):
    quiet = deploy(dep("a", max_position=2, max_orders_per_day=3))
    inst = start_strategy(quiet, "a")
    SCRIPT.extend([("buy",), ("buy",), ("buy",), ("exit", "done"), ("buy",), ("buy",), ("exit",)])
    for _ in range(7):
        tick(quiet)

    got = list(reversed(signals(quiet, inst["id"])))
    assert [(s["side"], s["quantity"], s["outcome"]) for s in got] == [
        ("BUY", 1, "signal_only"),
        ("BUY", 1, "signal_only"),
        ("BUY", 1, "blocked"),        # would make position 3 > max_position 2
        ("SELL", 2, "signal_only"),   # exit: above max_order_qty 1, still allowed
        ("BUY", 1, "blocked"),        # 3 orders today already
        ("BUY", 1, "blocked"),
    ]
    assert "max_position" in got[2]["message"] and "max_orders_per_day" in got[4]["message"]
    assert got[3]["reason"] == "done" and got[0]["price"] == 100.0
    assert instance(quiet, inst["id"])["position"] == 0
    assert quiet.get("/api/accounts/1/orders/history", headers=AUTH).json() == []  # nothing sent to the broker


def test_live_mode_needs_global_switch(deploy):
    quiet = deploy(dep("a", mode="live"))
    inst = start_strategy(quiet, "a")
    SCRIPT.append(("buy",))
    tick(quiet)
    [sig] = signals(quiet, inst["id"])
    assert sig["outcome"] == "blocked" and "AUTOTRADE_STRATEGIES_LIVE" in sig["message"]
    assert quiet.get("/api/strategies", headers=AUTH).json()["live_trading_enabled"] is False


# ---------- live mode (paper broker) ----------

def test_live_mode_places_orders_and_tracks_fills(deploy, monkeypatch):
    monkeypatch.setattr(settings, "strategies_live_enabled", True)
    quiet = deploy(dep("a", mode="live", quantity=2, max_position=2))
    inst = start_strategy(quiet, "a")
    SCRIPT.extend([("buy",), ("exit", "take profit")])
    tick(quiet)
    assert instance(quiet, inst["id"])["position"] == 2
    tick(quiet)

    placed = list(reversed(signals(quiet, inst["id"])))
    assert [(s["side"], s["outcome"]) for s in placed] == [("BUY", "placed"), ("SELL", "placed")]
    assert all(s["order_id"] for s in placed)
    assert instance(quiet, inst["id"])["position"] == 0
    # The orders went through the normal pipeline: history and the trades ledger.
    history = quiet.get("/api/accounts/1/orders/history", headers=AUTH).json()
    assert {o["id"] for o in history} == {s["order_id"] for s in placed}
    assert {o["strategy"] for o in history} == {inst["label"]}  # attributed to the strategy
    closed = quiet.get("/api/trades/closed", headers=AUTH).json()
    assert closed["totals"]["count"] == 1 and closed["positions"][0]["strategy"] == inst["label"]


def test_live_rejection_is_recorded(deploy, monkeypatch):
    monkeypatch.setattr(settings, "strategies_live_enabled", True)
    quiet = deploy(dep("a", mode="live", symbol="NSE:NOPE"))  # paper broker doesn't know it
    inst = start_strategy(quiet, "a")
    SCRIPT.append(("buy",))
    tick(quiet, symbol="NSE:NOPE")
    [sig] = signals(quiet, inst["id"])
    assert sig["outcome"] == "rejected" and "Unknown symbol" in sig["message"]
    assert instance(quiet, inst["id"])["position"] == 0


# ---------- failures, kill switch, restarts ----------

def test_exception_stops_strategy_with_error(deploy):
    quiet = deploy(dep("a"))
    inst = start_strategy(quiet, "a")
    SCRIPT.append(("raise",))
    tick(quiet)
    got = instance(quiet, inst["id"])
    assert got["status"] == "error" and "boom" in got["last_error"]
    assert inst["id"] not in quiet.app.state.strategy_runner.running


def test_stop_all(deploy):
    quiet = deploy(dep("a"), dep("b", symbol="NSE:TCS-EQ"))
    a, b = start_strategy(quiet, "a"), start_strategy(quiet, "b")
    assert sorted(quiet.post("/api/strategies/stop-all", headers=AUTH).json()["stopped"]) == [a["id"], b["id"]]
    assert {i["status"] for i in quiet.get("/api/strategies", headers=AUTH).json()["strategies"]} == {"stopped"}


def test_running_strategies_resume_after_restart(deploy):
    quiet = deploy(dep("a"))
    inst = start_strategy(quiet, "a")
    SCRIPT.append(("buy",))
    tick(quiet)

    fresh = deploy(dep("a"))  # same code, server restarted on the same database
    got = fresh.get(f"/api/strategies/{inst['id']}", headers=AUTH).json()
    assert got["status"] == "running"
    assert got["position"] == 1  # rebuilt from its signals


# ---------- building blocks ----------

def test_bar_builder():
    bars = BarBuilder(60)
    t = lambda sec, p: Tick(SYM, p, datetime.fromtimestamp(sec, tz=timezone.utc))
    assert bars.add(t(0, 10)) is None
    assert bars.add(t(30, 12)) is None
    assert bars.add(t(59, 9)) is None
    done = bars.add(t(61, 11))  # next minute closes the first bar
    assert (done.open, done.high, done.low, done.close, done.ticks) == (10, 12, 9, 9, 3)


class FakeCtx:
    def __init__(self):
        self.position, self.calls = 0, []

    async def buy(self, quantity=None, reason=None):
        self.calls.append("buy"); self.position += 1

    async def sell(self, quantity=None, reason=None):
        self.calls.append("sell"); self.position -= 1

    async def exit(self, reason=None):
        self.calls.append("exit"); self.position = 0


def test_sma_crossover_logic():
    async def run_closes(closes, **params):
        ctx = FakeCtx()
        strat = SmaCrossover(SmaCrossover.Params(fast=2, slow=3, **params), ctx)
        await strat.on_start()
        for i, c in enumerate(closes):
            await strat.on_bar(Bar(SYM, datetime.now(timezone.utc), 60, c, c, c, c, 1))
        return ctx.calls

    import asyncio
    falling_then_rising_then_falling = [10, 9, 8, 7, 9, 11, 12, 9, 6, 5]
    assert asyncio.run(run_closes(falling_then_rising_then_falling)) == ["buy", "exit"]
    assert asyncio.run(run_closes(falling_then_rising_then_falling, allow_short=True)) == [
        "buy", "exit", "sell"
    ]


def test_exits_are_never_blocked_by_limits(deploy):
    quiet = deploy(dep("a", quantity=1, max_position=1, max_orders_per_day=1))
    inst = start_strategy(quiet, "a")
    SCRIPT.extend([("buy",), ("sell", 2), ("exit",)])  # flip attempt, then exit
    for _ in range(3):
        tick(quiet)
    got = list(reversed(signals(quiet, inst["id"])))
    assert [(s["side"], s["quantity"], s["outcome"]) for s in got] == [
        ("BUY", 1, "signal_only"),
        ("SELL", 2, "blocked"),       # a flip opens new risk, so limits apply
        ("SELL", 1, "signal_only"),   # exit allowed despite max_orders_per_day=1
    ]


def test_failed_startup_closes_the_broker(deploy, monkeypatch):
    """Regression: a startup error after the broker connected left its socket threads
    running, so the server process hung instead of exiting."""
    deploy(dep("a")).__exit__(None, None, None)  # a user with a paper account exists
    stopped = []

    class Broker:
        async def start(self): pass
        async def stop(self): stopped.append(True)
        async def subscribe(self, symbols): pass
        def on_order_update(self, fn): pass
        def on_tick(self, fn): pass

    monkeypatch.setattr("app.accounts.registry.create_broker", lambda *a, **kw: Broker())
    monkeypatch.setattr(deployments, "DEPLOYMENTS", [dep("same"), dep("same")])  # duplicate keys
    with pytest.raises(ValueError, match="Duplicate deployment keys"):
        with TestClient(app):
            pass
    assert stopped == [True]


# ---------- symbol and timeframe chosen in the app ----------

def add(client, key, symbol, timeframe="1m", account_id=ACCOUNT, headers=AUTH):
    return client.post("/api/strategies", json={"deployment_key": key, "account_id": account_id,
                                                "symbol": symbol, "timeframe": timeframe}, headers=headers)


def test_add_copies_on_other_symbols_and_timeframes(deploy):
    c = deploy(dep("a"))
    r = add(c, "a", "NSE:TCS-EQ", "5m")
    assert r.status_code == 201, r.text
    copy = r.json()
    assert (copy["symbol"], copy["timeframe"], copy["status"], copy["mode"]) == ("NSE:TCS-EQ", "5m", "stopped", "signal")
    assert copy["warning"] is None  # paper broker knows the symbol

    assert add(c, "a", "NSE:TCS-EQ", "5m").status_code == 409  # same target twice
    assert add(c, "a", "NSE:NOPE").status_code == 422  # broker doesn't know it
    assert add(c, "a", "NSE:TCS-EQ", "2m").status_code == 422  # not a timeframe

    both = [i for i in c.get("/api/strategies", headers=AUTH).json()["strategies"] if i["deployment_key"] == "a"]
    assert {(i["symbol"], i["timeframe"]) for i in both} == {(SYM, "5m"), ("NSE:TCS-EQ", "5m")}


def test_timeframe_sets_the_bar_length(deploy):
    c = deploy(dep("a"))
    copy = add(c, "a", "NSE:TCS-EQ", "15m").json()
    c.post(f"/api/strategies/{copy['id']}/start", headers=AUTH)
    running = c.app.state.strategy_runner.running[copy["id"]]
    assert running.strategy.bar_seconds == 900 and running.strategy.ctx.timeframe == "15m"


def test_strategy_can_restrict_timeframes(deploy):
    from app.strategies.builtin.intraday_breakout import IntradayBreakout

    c = deploy(dep("brk", strategy=IntradayBreakout))
    assert add(c, "brk", "NSE:TCS-EQ", "60m").status_code == 422  # breakout allows up to 15m
    assert add(c, "brk", "NSE:TCS-EQ", "5m").status_code == 201
    with pytest.raises(ValueError, match="supports"):
        dep("x", strategy=IntradayBreakout, timeframe="60m")


def test_change_symbol_and_timeframe_while_stopped(deploy):
    c = deploy(dep("a"))
    inst = by_key(c, "a")
    r = c.patch(f"/api/strategies/{inst['id']}", json={"symbol": "NSE:INFY-EQ", "timeframe": "3m"}, headers=AUTH)
    assert (r.json()["symbol"], r.json()["timeframe"]) == ("NSE:INFY-EQ", "3m")
    assert c.patch(f"/api/strategies/{inst['id']}", json={"symbol": "NSE:NOPE"}, headers=AUTH).status_code == 422

    other = add(c, "a", "NSE:TCS-EQ", "3m").json()
    clash = c.patch(f"/api/strategies/{other['id']}", json={"symbol": "NSE:INFY-EQ"}, headers=AUTH)
    assert clash.status_code == 409  # #1 already runs there

    start_strategy(c, "a")
    assert c.patch(f"/api/strategies/{inst['id']}", json={"timeframe": "5m"}, headers=AUTH).status_code == 409


def test_archive_and_bring_back(deploy):
    c = deploy(dep("a"))
    copy = add(c, "a", "NSE:TCS-EQ").json()
    SCRIPT.append(("buy",))
    c.post(f"/api/strategies/{copy['id']}/start", headers=AUTH)
    tick(c, symbol="NSE:TCS-EQ")
    assert c.delete(f"/api/strategies/{copy['id']}", headers=AUTH).status_code == 409  # running
    c.post(f"/api/strategies/{copy['id']}/stop", headers=AUTH)

    assert c.delete(f"/api/strategies/{copy['id']}", headers=AUTH).json()["status"] == "archived"
    visible = [i["id"] for i in c.get("/api/strategies", headers=AUTH).json()["strategies"]]
    assert copy["id"] not in visible
    assert c.post(f"/api/strategies/{copy['id']}/start", headers=AUTH).status_code == 409

    back = add(c, "a", "NSE:TCS-EQ").json()
    assert back["id"] == copy["id"] and back["status"] == "stopped"
    assert len(signals(c, back["id"])) == 1  # history kept


def test_code_changes_apply_to_every_instance(deploy):
    c = deploy(dep("a"))
    copy = add(c, "a", "NSE:TCS-EQ", "5m").json()
    start_strategy(c, "a")
    c.post(f"/api/strategies/{copy['id']}/start", headers=AUTH)

    c = deploy(dep("a", quantity=2))  # admin edits the deployment
    rows = [i for i in c.get("/api/strategies", headers=AUTH).json()["strategies"] if i["deployment_key"] == "a"]
    assert {(i["symbol"], i["timeframe"], i["quantity"], i["status"]) for i in rows} == {
        (SYM, "5m", 2, "stopped"), ("NSE:TCS-EQ", "5m", 2, "stopped")}  # app's choices kept


def test_code_creates_no_instances(deploy, monkeypatch):
    """Code is the catalog; instances exist only where a user added them, on their account."""
    c = deploy()
    monkeypatch.setattr(deployments, "DEPLOYMENTS", [dep("a")])
    c.__exit__(None, None, None)
    with TestClient(app) as fresh:  # restart with a deployment in code
        body = fresh.get("/api/strategies", headers=AUTH).json()
        assert [d["key"] for d in body["deployments"]] == ["a"] and body["strategies"] == []
        r = add(fresh, "a", SYM)
        assert r.status_code == 201 and r.json()["account_id"] == ACCOUNT


def test_orders_per_day_starts_again_the_next_day(deploy, monkeypatch):
    """max_orders_per_day counts per IST day, also for an instance running past midnight."""
    import app.strategies.runner as runner_module
    from datetime import date

    monkeypatch.setattr(runner_module, "_today_ist", lambda: date(2026, 10, 26))
    quiet = deploy(dep("a", max_orders_per_day=1, max_position=5))
    inst = start_strategy(quiet, "a")
    SCRIPT.extend([("buy",), ("buy",)])
    tick(quiet)
    tick(quiet)
    assert [s["outcome"] for s in reversed(signals(quiet, inst["id"]))] == ["signal_only", "blocked"]

    monkeypatch.setattr(runner_module, "_today_ist", lambda: date(2026, 10, 27))  # midnight passes
    assert instance(quiet, inst["id"])["orders_today"] == 0
    SCRIPT.append(("buy",))
    tick(quiet)
    assert signals(quiet, inst["id"])[0]["outcome"] == "signal_only"
    assert instance(quiet, inst["id"])["orders_today"] == 1


# ---------- position sizing chosen in the app ----------

def test_sizing_fixed_quantity_or_capital_per_trade(deploy):
    c = deploy(dep("a", quantity=1, max_order_qty=10, max_position=10, max_orders_per_day=20))
    inst = by_key(c, "a")
    assert (inst["size_mode"], inst["fixed_quantity"], inst["sized_quantity"]) == ("fixed", None, 1)  # code's quantity

    def patch(**body):
        return c.patch(f"/api/strategies/{inst['id']}", json=body, headers=AUTH)

    assert patch(fixed_quantity=11).status_code == 422  # above max_order_qty
    assert patch(size_mode="capital").status_code == 422  # needs an amount
    assert patch(fixed_quantity=3).json()["sized_quantity"] == 3

    start_strategy(c, "a")
    SCRIPT.extend([("buy",), ("exit",)])
    tick(c)
    tick(c)
    c.post(f"/api/strategies/{inst['id']}/stop", headers=AUTH)

    r = patch(size_mode="capital", capital_per_trade=450)
    assert r.status_code == 200, r.text
    assert (r.json()["size_mode"], r.json()["capital_per_trade"], r.json()["sized_quantity"]) == ("capital", 450, None)
    start_strategy(c, "a")
    SCRIPT.extend([("buy",), ("exit",)])
    tick(c, price=100.0)  # ₹450 buys 4 at 100
    tick(c, price=100.0)
    c.post(f"/api/strategies/{inst['id']}/stop", headers=AUTH)

    r = patch(capital_per_trade=5000)
    start_strategy(c, "a")
    SCRIPT.extend([("buy",)])
    tick(c, price=100.0)  # 50 units, capped at max_order_qty 10

    got = [(s["side"], s["quantity"]) for s in reversed(signals(c, inst["id"]))]
    assert got == [("BUY", 3), ("SELL", 3), ("BUY", 4), ("SELL", 4), ("BUY", 10)]
    assert instance(c, inst["id"])["sized_quantity"] == 10


def test_sized_quantity_rounds_to_lots_and_caps():
    from decimal import Decimal

    from app.strategies.models import StrategyInstance
    from app.strategies.runner import sized_quantity

    row = StrategyInstance(quantity=65, max_order_qty=650, max_position=650, size_mode="capital",
                           capital_per_trade=Decimal("5000000"))
    assert sized_quantity(row, 26000.0, lot=65) == 130  # 192 units -> 2 lots
    assert sized_quantity(row, 26000.0, lot=1) == 192
    assert sized_quantity(row, None) == 0
    row.capital_per_trade = Decimal("1000000")
    assert sized_quantity(row, 26000.0, lot=65) == 0  # can't pay for one lot
    row.size_mode, row.fixed_quantity = "fixed", None
    assert sized_quantity(row, 26000.0) == 65
