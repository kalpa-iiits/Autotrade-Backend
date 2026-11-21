import asyncio
from datetime import date, datetime, timedelta

from app.brokers.base import Candle, OptionContract
from app.orders.schemas import Product
from app.strategies.base import TIMEFRAMES, Tick
from app.strategies.backtest import Simulation
from app.strategies.deployment import Deployment
from app.strategies.builtin.nifty_trailing_reversal_hedged_futures import (
    NiftyTrailingReversalOptimizedHedgedFutures,
)
from app.strategies.builtin.nifty_trailing_reversal_itm_option_seller import (
    FrozenReset,
    NiftyTrailingReversalOptimizedITMOptionSeller,
    QuoteSnapshot,
    ShortWeeklyOptionLegs,
    classify_reset,
)
from app.strategies.builtin.nifty_trailing_reversal_optimized import IST


EXPIRY = date(2026, 11, 24)
AT = datetime(2026, 11, 20, 10, 0, tzinfo=IST)


class OptionSellerContext:
    def __init__(self, positions=None, quantity=65):
        self.positions = dict(positions or {})
        self.quantity = quantity
        self.last_price = 25_000
        self.order_pending = False
        self.strategy_state = {}
        self.orders = []
        self.logs = []
        self.quotes = {}
        self.contracts = [
            OptionContract(f"NSE:NIFTY26N24{strike}{kind}", kind, float(strike), EXPIRY, 65)
            for strike in range(23_500, 26_501, 100)
            for kind in ("CE", "PE")
        ]

    async def option_contracts(self):
        return self.contracts

    async def quote(self, symbol):
        return self.quotes.get(symbol)

    async def save_strategy_state(self, state):
        self.strategy_state = dict(state)

    async def buy(self, quantity=None, reason=None, symbol=None, product=None):
        self.orders.append(("BUY", quantity, symbol, product))
        updated = self.positions.get(symbol, 0) + quantity
        if updated:
            self.positions[symbol] = updated
        else:
            self.positions.pop(symbol, None)

    async def sell(self, quantity=None, reason=None, symbol=None, product=None):
        self.orders.append(("SELL", quantity, symbol, product))
        updated = self.positions.get(symbol, 0) - quantity
        if updated:
            self.positions[symbol] = updated
        else:
            self.positions.pop(symbol, None)

    def log(self, message):
        self.logs.append(message)


def test_long_signal_shorts_exactly_one_current_week_900_itm_put():
    ctx = OptionSellerContext()
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2425900PE", Product.DELIVERY),
    ]
    assert legs.position == 65


def test_strategy_forces_overnight_product_even_when_caller_requests_intraday():
    ctx = OptionSellerContext()
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)

    asyncio.run(legs.buy(reason="initial LONG", product=Product.INTRADAY))

    assert ctx.orders
    assert all(order[3] == Product.DELIVERY for order in ctx.orders)


def test_frontend_quantity_scales_short_and_margin_hedge_together():
    ctx = OptionSellerContext(quantity=130)
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert ctx.orders == [
        ("BUY", 130, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
        ("SELL", 130, "NSE:NIFTY26N2425900PE", Product.DELIVERY),
    ]
    assert legs.position == 130


def test_selection_prefers_liquid_100_point_strike_over_exact_50_point_target():
    ctx = OptionSellerContext()
    ctx.last_price = 25_050  # exact 900-ITM PE target is the less-liquid 25,950 strike
    ctx.contracts.append(OptionContract(
        "NSE:NIFTY26N2425950PE", "PE", 25_950, EXPIRY, 65,
    ))
    # 25,900 and 26,000 are equally distant; the normal deeper-ITM tie-break chooses 26,000.
    ctx.quotes["NSE:NIFTY26N2426000PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2426000PE", Product.DELIVERY),
    ]


def test_selection_falls_back_to_non_100_strike_only_when_no_100_strike_is_listed():
    ctx = OptionSellerContext()
    ctx.last_price = 25_050
    ctx.contracts = [OptionContract(
        "NSE:NIFTY26N2425950PE", "PE", 25_950, EXPIRY, 65,
    ), OptionContract(
        "NSE:NIFTY26N2424050PE", "PE", 24_050, EXPIRY, 65,
    )]
    ctx.quotes["NSE:NIFTY26N2425950PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26N2424050PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2425950PE", Product.DELIVERY),
    ]


def test_short_entry_waits_when_same_expiry_1000_otm_hedge_is_unavailable():
    ctx = OptionSellerContext()
    ctx.contracts = [OptionContract(
        "NSE:NIFTY26N2425900PE", "PE", 25_900, EXPIRY, 65,
    )]
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert not ctx.orders
    assert any("margin hedge" in message for message in ctx.logs)


def test_reversal_covers_put_before_shorting_one_900_itm_call():
    pe = "NSE:NIFTY26N2425900PE"
    ctx = OptionSellerContext({pe: -65})
    ctx.quotes["NSE:NIFTY26N2424100CE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(-1)

    async def reverse():
        await legs.advance(AT, "reverse SHORT")
        await legs.advance(AT, "reverse SHORT")

    asyncio.run(reverse())

    assert ctx.orders == [
        ("BUY", 65, pe, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2426000CE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2424100CE", Product.DELIVERY),
    ]
    assert legs.position == -65


def test_reset_uses_frozen_level_and_replaces_on_downward_crossing_without_two_lots():
    old = "NSE:NIFTY26N2425900PE"
    ctx = OptionSellerContext({old: -65})
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.frozen = classify_reset("PE", 25_900, 25_000, 950, EXPIRY, AT)

    async def reset():
        ctx.quotes[old] = 905
        await legs.monitor_reset(AT)
        assert not ctx.orders

        ctx.last_price = 25_100
        ctx.quotes["NSE:NIFTY26N2426000PE"] = 960
        ctx.quotes[old] = 900
        await legs.monitor_reset(AT)
        await legs.advance(AT)

    asyncio.run(reset())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2424100PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2426000PE", Product.DELIVERY),
    ]
    assert min(ctx.positions.values()) == -65
    assert sum(abs(quantity) for quantity in ctx.positions.values()) == 130


def test_premium_gap_below_reset_stays_pending_until_replacement_quote_appears():
    old = "NSE:NIFTY26N2425900PE"
    fresh = "NSE:NIFTY26N2426000PE"
    ctx = OptionSellerContext({old: -65})
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.frozen = classify_reset("PE", 25_900, 25_000, 950, EXPIRY, AT)
    legs.reset_armed = True

    async def gap_and_retry():
        # Overnight/favorable premium gap: 950 to 700 crosses the frozen ₹900 level.
        ctx.last_price = 25_100
        ctx.quotes[old] = 700
        await legs.monitor_reset(AT)
        assert legs.reset_pending
        assert not ctx.orders

        # No second crossing is required. The latched reset retries when the new strike gets a
        # quote, then performs cover-before-replacement and clears the latch.
        ctx.quotes[fresh] = 960
        await legs.monitor_reset(AT + timedelta(seconds=5))
        await legs.advance(AT + timedelta(seconds=5))

    asyncio.run(gap_and_retry())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2424100PE", Product.DELIVERY),
        ("SELL", 65, fresh, Product.DELIVERY),
    ]
    assert not legs.reset_pending


def test_premium_reset_explicitly_keeps_the_held_expiry():
    later_expiry = date(2026, 11, 26)
    old = "NSE:NIFTY26N2625900PE"
    replacement = "NSE:NIFTY26N2626000PE"
    ctx = OptionSellerContext({old: -65})
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26N26{strike}PE", "PE", float(strike), later_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    ctx.last_price = 25_100
    ctx.quotes[old] = 700
    ctx.quotes[replacement] = 960
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.frozen = FrozenReset(4, 900, 50, 0.02, "HIGH", 900, AT, later_expiry)
    legs.reset_armed = True

    async def reset():
        await legs.monitor_reset(AT)
        await legs.advance(AT)

    asyncio.run(reset())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2624100PE", Product.DELIVERY),
        ("SELL", 65, replacement, Product.DELIVERY),
    ]


def test_missing_replacement_quote_keeps_existing_short_untouched():
    pe = "NSE:NIFTY26N2425900PE"
    ctx = OptionSellerContext({pe: -65})
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(-1)

    asyncio.run(legs.advance(AT, "reverse SHORT"))

    assert not ctx.orders
    assert ctx.positions == {pe: -65}
    assert legs.position == 65
    assert any("unavailable" in message for message in ctx.logs)


def test_expiry_cover_waits_until_next_session_then_reopens_and_reclassifies():
    expiry_at = datetime(2026, 11, 24, 15, 10, tzinfo=IST)
    old = "NSE:NIFTY26N2425900PE"
    old_hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({old: -65, old_hedge: 65})
    next_expiry = date(2026, 12, 1)
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26D01{strike}PE", "PE", float(strike), next_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    close_candidate = "NSE:NIFTY26D0125900PE"
    gap_candidate = "NSE:NIFTY26D0126100PE"
    ctx.quotes[close_candidate] = 960
    ctx.quotes[gap_candidate] = 960
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = old_hedge
    legs.set_desired(1)

    async def roll():
        await legs.monitor_expiry(expiry_at)
        await legs.advance(expiry_at)
        assert ctx.positions == {"NSE:NIFTY26D0124000PE": 65}
        # Reselect from the next session's spot rather than carrying the 25,900 strike selected
        # as the pre-close safety candidate through this 200-point overnight gap.
        ctx.last_price = 25_200
        await legs.advance(datetime(2026, 11, 25, 9, 15, tzinfo=IST))

    asyncio.run(roll())

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26D0124000PE", Product.DELIVERY),
        ("BUY", 65, old, Product.DELIVERY),
        ("SELL", 65, old_hedge, Product.DELIVERY),
        ("SELL", 65, gap_candidate, Product.DELIVERY),
    ]
    assert legs.frozen is not None and legs.frozen.classified_at.date() == date(2026, 11, 25)


def test_expiry_reopen_processes_opening_gap_signal_before_choosing_option_type():
    gap_open = datetime(2026, 11, 20, 9, 15, tzinfo=IST)
    ctx = OptionSellerContext()
    ctx.strategy_state = {
        "version": 1,
        # Checkpoints written before the display-name change must remain restorable.
        "strategy": "nifty_trailing_reversal_optimized_itm_option_seller",
        "bar_seconds": 300,
        "params": {"itm_points": 900.0},
        "seeded": True,
        "direction": 1,
        "stop": 24_900.0,
        "seed_pending": False,
        "reversal_pending": False,
        "pre_reversal_stop": None,
        "reversal_reference": None,
        "fill_bucket": None,
        "pending_trails": [],
        "weekly_option_leg": {
            "frozen": None,
            "last_premium": None,
            "reset_armed": False,
            "reset_pending": False,
            "roll_after": gap_open.date().isoformat(),
            "pending_audit": None,
        },
    }
    # The continued LONG would open a PE, but this gap is below its carried 24,900 stop. The
    # first spot tick must reverse the virtual signal and open a 900-ITM CE directly.
    ce = "NSE:NIFTY26N2423100CE"
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26N24{strike}CE", "CE", float(strike), EXPIRY, 65)
        for strike in range(22_500, 23_500, 100)
    ]
    ctx.quotes[ce] = 950
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller(
        NiftyTrailingReversalOptimizedITMOptionSeller.Params(itm_points=900), ctx, 300,
    )

    async def reopen():
        await strategy.on_start()
        assert not ctx.orders
        ctx.last_price = 24_000
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 24_000, gap_open))

    asyncio.run(reopen())

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
        ("SELL", 65, ce, Product.DELIVERY),
    ]
    assert strategy.ctx.desired_direction == -1


def test_rules_keep_optimized_signal_triggers_unchanged():
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller
    params = strategy.Params(itm_points=900)
    rules = strategy.rules(params)
    risk = next(rule for rule in rules if rule["kind"] == "risk")

    assert strategy.initial_stop_pct == 0.4
    assert strategy.trailing_stop_pct == 0.4
    assert strategy.cadence_minutes == 10
    assert any("2–3 LOW ₹900/HIGH ₹700" in condition for condition in risk["conditions"])


def test_tester_exposes_all_supported_timeframes_but_keeps_five_minute_default():
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller

    assert strategy.timeframes == tuple(TIMEFRAMES)
    assert strategy.timeframes == ("1m", "3m", "5m", "10m", "15m", "30m", "60m")
    assert strategy.default_timeframe == "5m"


def test_dte_and_iv_grid_selects_every_published_reset_bucket():
    def result(days, premium):
        expiry = AT.date() + timedelta(days=days)
        return classify_reset("PE", 25_900, 25_000, premium, expiry, AT)

    assert result(0, 900).reset_level == 850
    assert result(1, 900).reset_level == 850
    assert (result(2, 900).iv_regime, result(2, 900).reset_level) == ("LOW", 900)
    assert (result(2, 1_200).iv_regime, result(2, 1_200).reset_level) == ("HIGH", 700)
    assert (result(4, 900).iv_regime, result(4, 900).reset_level) == ("LOW", 850)
    assert (result(4, 1_200).iv_regime, result(4, 1_200).reset_level) == ("HIGH", 900)


def test_frozen_reset_state_survives_adapter_restart():
    ctx = OptionSellerContext()
    first = ShortWeeklyOptionLegs(ctx, 900)
    first.frozen = classify_reset("PE", 25_900, 25_000, 950, EXPIRY, AT)
    first.last_premium = 921
    asyncio.run(first._save_leg_state())

    restored = ShortWeeklyOptionLegs(ctx, 900)

    assert restored.frozen == first.frozen
    assert restored.last_premium == 921


def test_structured_audit_adds_actual_fill_price_when_broker_confirms():
    class FillContext(OptionSellerContext):
        async def latest_option_fill(self, symbol, after):
            return 947.25 if self.positions.get(symbol) == -65 else None

    ctx = FillContext()
    symbol = "NSE:NIFTY26N2425900PE"
    ctx.quotes[symbol] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    async def enter_and_confirm():
        await legs.advance(AT, "entry")
        await legs.advance(AT + timedelta(seconds=1))

    asyncio.run(enter_and_confirm())

    filled = [message for message in ctx.logs if '"stage":"filled"' in message]
    assert len(filled) == 1
    assert '"order_fill_price":947.25' in filled[0]
    assert '"frozen_reset_level":900.0' in filled[0]


def test_stale_replacement_quote_keeps_old_position():
    class StaleContext(OptionSellerContext):
        async def executable_quote(self, symbol, side):
            return QuoteSnapshot(950, AT - timedelta(seconds=16), "stale-test")

    old = "NSE:NIFTY26N2425900PE"
    ctx = StaleContext({old: -65})
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(-1)

    asyncio.run(legs.advance(AT, "reversal"))

    assert ctx.positions == {old: -65}
    assert not ctx.orders
    assert any("stale" in message for message in ctx.logs)


def test_opening_gap_reversal_stays_latched_and_uses_gap_reference_until_quote_arrives():
    old = "NSE:NIFTY26N2425900PE"
    fresh = "NSE:NIFTY26N2423100CE"
    ctx = OptionSellerContext({old: -65})
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26N24{strike}CE", "CE", float(strike), EXPIRY, 65)
        for strike in range(22_500, 23_500, 100)
    ]
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller(
        NiftyTrailingReversalOptimizedITMOptionSeller.Params(itm_points=900), ctx, 300,
    )
    prior_session = datetime(2026, 11, 19, 15, 25, tzinfo=IST)
    gap_open = datetime(2026, 11, 20, 9, 15, tzinfo=IST)

    async def replay():
        await strategy.on_start()
        ctx.last_price = 25_000
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, prior_session))
        assert strategy.stop == 24_900
        assert ctx.orders == [
            ("BUY", 65, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
        ]
        ctx.orders.clear()

        # The first tradable tick gaps through the stop. With no CE bid yet, retain the PE but
        # keep the reversal pending at 24,000 instead of reverting to the old 24,900 stop.
        ctx.last_price = 24_000
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 24_000, gap_open))
        assert not ctx.orders
        assert strategy._reversal_pending
        assert strategy._reversal_reference == 24_000

        # As soon as the quote appears, complete cover then replacement. A later tick confirms
        # the new short stop still derives from the original gap reference: 24,000 * 1.004.
        ctx.quotes[fresh] = 950
        ctx.last_price = 24_020
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 24_020,
                                    gap_open + timedelta(seconds=1)))
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 24_020,
                                    gap_open + timedelta(seconds=2)))

    asyncio.run(replay())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
        ("SELL", 65, fresh, Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
    ]
    assert strategy.stop == 24_096
    assert not strategy._reversal_pending


def test_option_seller_backtest_signals_exactly_match_perfected_optimized_futures_strategy():
    first = datetime(2026, 11, 2, 9, 15, tzinfo=IST)
    candles = [
        Candle(first, 24_950, 25_000, 24_900, 24_980),
        Candle(first + timedelta(minutes=5), 25_000, 25_100, 24_700, 24_800),
        Candle(first + timedelta(minutes=10), 24_800, 25_100, 24_700, 24_750),
        Candle(first + timedelta(minutes=15), 24_750, 24_780, 24_500, 24_600),
        Candle(first + timedelta(minutes=20), 24_600, 24_900, 24_550, 24_850),
    ]

    async def replay(strategy, params):
        deployment = Deployment(
            key=f"test-{strategy.name}",
            strategy=strategy,
            symbol="NSE:NIFTY50-INDEX",
            timeframe="5m",
            params=params,
            quantity=65,
            max_order_qty=1_800,
            max_position=1_800,
            max_orders_per_day=100,
            backtest_fixed_quantity=65,
        )
        simulation = Simulation(deployment, deployment.symbol, "5m", daily=[])
        await simulation.run(candles)
        return [
            (trade.direction, trade.entry_at, trade.entry_price, trade.exit_at,
             trade.exit_price, trade.quantity)
            for trade in simulation.trades
        ]

    futures = asyncio.run(replay(
        NiftyTrailingReversalOptimizedHedgedFutures,
        NiftyTrailingReversalOptimizedHedgedFutures.Params(hedge_points=500),
    ))
    options = asyncio.run(replay(
        NiftyTrailingReversalOptimizedITMOptionSeller,
        NiftyTrailingReversalOptimizedITMOptionSeller.Params(itm_points=900),
    ))

    assert options == futures
