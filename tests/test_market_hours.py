"""Periodic broker checks only run in market hours (plus one check after the close)."""

import asyncio
import time
from datetime import datetime
from types import SimpleNamespace

import app.core.broker_service as broker_service_mod
import app.strategies.runner as runner_mod
from app.core import market
from app.core.broker_service import BrokerService
from app.core.config import settings
from app.strategies.runner import StrategyRunner
from tests.conftest import run
from tests.test_reliability import _break_order_book, _status, _wait_running  # noqa: F401
from tests.test_strategies import deploy, dep, start_strategy  # noqa: F401

IST = market.IST


def test_window_is_weekdays_only_and_empty_means_always(monkeypatch):
    monkeypatch.setattr(settings, "background_hours", "09:00-15:45")
    fri = lambda h, m: datetime(2026, 11, 6, h, m, tzinfo=IST)  # noqa: E731
    assert not market.in_market_hours(fri(8, 59))
    assert market.in_market_hours(fri(9, 0))
    assert market.in_market_hours(fri(15, 44))
    assert not market.in_market_hours(fri(15, 45))
    assert not market.in_market_hours(datetime(2026, 11, 7, 11, 0, tzinfo=IST))  # Saturday
    # Friday after the close -> Monday 09:00
    assert market.seconds_until_market_hours(fri(15, 45)) == 65.25 * 3600
    assert market.seconds_until_market_hours(fri(10, 0)) == 0

    monkeypatch.setattr(settings, "background_hours", "")
    assert market.in_market_hours(datetime(2026, 11, 7, 3, 0, tzinfo=IST))


def _drive(loop_fn, fake_self, schedule, monkeypatch, module):
    """Run a periodic loop through a scripted sequence of open/closed market states."""
    states = iter(schedule)
    current = {"open": True}

    def in_hours():
        return current["open"]

    monkeypatch.setattr(module, "in_market_hours", in_hours)

    async def go():
        task = asyncio.create_task(loop_fn(fake_self))
        for state in states:
            current["open"] = state
            await asyncio.sleep(0.03)
        task.cancel()

    current["open"] = schedule[0]
    asyncio.run(go())


def test_order_book_check_stops_after_one_check_past_the_close(monkeypatch):
    calls = []

    async def resync():
        calls.append(time.monotonic())

    fake = SimpleNamespace(settings=SimpleNamespace(resync_interval=0.01), resync=resync)
    _drive(BrokerService._resync_loop, fake, [True, False, False], monkeypatch, broker_service_mod)
    open_calls = len(calls)
    assert open_calls >= 1

    calls.clear()
    _drive(BrokerService._resync_loop, fake, [False, False], monkeypatch, broker_service_mod)
    assert calls == []  # started after hours: nothing


def test_order_book_check_runs_exactly_once_after_close(monkeypatch):
    calls, current = [], {"open": True}
    monkeypatch.setattr(broker_service_mod, "in_market_hours", lambda: current["open"])

    async def resync():
        calls.append(current["open"])
        current["open"] = False  # the market closes right after the first check

    fake = SimpleNamespace(settings=SimpleNamespace(resync_interval=0.005), resync=resync)

    async def go():
        task = asyncio.create_task(BrokerService._resync_loop(fake))
        await asyncio.sleep(0.1)
        task.cancel()

    asyncio.run(go())
    assert calls == [True, False]  # one in hours, one last after the close, then none


def test_strategy_check_skipped_outside_market_hours(monkeypatch):
    calls = []

    async def reconcile():
        calls.append(1)

    fake = SimpleNamespace(settings=SimpleNamespace(strategy_reconcile_interval=0.01),
                           _reconcile_running=reconcile)
    _drive(StrategyRunner._reconciliation_loop, fake, [False, False, False], monkeypatch, runner_mod)
    assert calls == []
    _drive(StrategyRunner._reconciliation_loop, fake, [True], monkeypatch, runner_mod)
    assert calls


def test_retry_waits_for_market_hours_unless_the_account_reconnects(deploy, monkeypatch):
    c = deploy(dep("after-hours"))
    inst = start_strategy(c, "after-hours")
    runner = c.app.state.strategy_runner
    service = c.app.state.accounts.get(1).service
    service.name = "fyers"
    restore = _break_order_book(service, monkeypatch)
    run(c, runner._reconcile_running)  # stopped, waiting to retry
    restore()

    monkeypatch.setattr(runner_mod, "in_market_hours", lambda: False)
    monkeypatch.setattr(runner_mod, "seconds_until_market_hours", lambda: 3600.0)

    async def make_due():
        now = asyncio.get_running_loop().time()
        runner._retries = {i: (now, d) for i, (_, d) in runner._retries.items()}
        runner._retry_wake.set()

    run(c, make_due)
    assert not _wait_running(c, inst["id"], timeout=0.3)  # held: market closed

    async def next_try_in():
        return runner._retries[inst["id"]][0] - asyncio.get_running_loop().time()
    assert run(c, next_try_in) > 3000  # moved to the market's next open

    run(c, runner.retry_now)  # e.g. the 08:45 login reconnected the account
    assert _wait_running(c, inst["id"])
