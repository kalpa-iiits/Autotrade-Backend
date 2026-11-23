import asyncio
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.brokers.base import Candle, OptionContract
from app.orders.schemas import Product
from app.strategies.base import Tick
from app.strategies.backtest import Simulation
from app.strategies.deployment import Deployment
from app.strategies.builtin.nifty_trailing_reversal_hedged_futures import (
    NiftyTrailingReversalOptimizedHedgedFutures,
)
from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
    SyntheticWeeklyOptionLegs,
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
        self.price_guards = []
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

    async def buy(self, quantity=None, reason=None, symbol=None, product=None,
                  option_price_guard=None):
        self.orders.append(("BUY", quantity, symbol, product))
        self.price_guards.append(("BUY", symbol, option_price_guard))
        updated = self.positions.get(symbol, 0) + quantity
        if updated:
            self.positions[symbol] = updated
        else:
            self.positions.pop(symbol, None)

    async def sell(self, quantity=None, reason=None, symbol=None, product=None,
                   option_price_guard=None):
        self.orders.append(("SELL", quantity, symbol, product))
        self.price_guards.append(("SELL", symbol, option_price_guard))
        updated = self.positions.get(symbol, 0) - quantity
        if updated:
            self.positions[symbol] = updated
        else:
            self.positions.pop(symbol, None)

    def log(self, message):
        self.logs.append(message)


@pytest.mark.parametrize(
    ("direction", "quotes", "expected"),
    [
        (
            1,
            {
                "NSE:NIFTY26N2425000CE": 210,
                "NSE:NIFTY26N2425000PE": 205,
                "NSE:NIFTY26N2424200PE": 20,
            },
            [
                ("BUY", 65, "NSE:NIFTY26N2424200PE", Product.DELIVERY),
                ("BUY", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
                ("SELL", 65, "NSE:NIFTY26N2425000PE", Product.DELIVERY),
            ],
        ),
        (
            -1,
            {
                "NSE:NIFTY26N2425000CE": 210,
                "NSE:NIFTY26N2425000PE": 205,
                "NSE:NIFTY26N2425800CE": 20,
            },
            [
                ("BUY", 65, "NSE:NIFTY26N2425800CE", Product.DELIVERY),
                ("BUY", 65, "NSE:NIFTY26N2425000PE", Product.DELIVERY),
                ("SELL", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
            ],
        ),
    ],
)
def test_13l_synthetic_pair_keeps_the_existing_otm_margin_hedge(direction, quotes, expected):
    ctx = OptionSellerContext()
    ctx.quotes.update(quotes)
    legs = SyntheticWeeklyOptionLegs(ctx)
    legs.set_desired(direction)

    async def enter():
        await legs.advance(AT, "initial synthetic")
        await legs.advance(AT, "initial synthetic")
        await legs.advance(AT, "initial synthetic")

    asyncio.run(enter())

    assert ctx.orders == expected
    assert legs.position == direction * 65
    assert legs.hedge_points == 800


def test_13l_uses_baskets_for_atm_entry_and_complete_exit_after_hedge_fill():
    class BasketContext(OptionSellerContext):
        def __init__(self):
            super().__init__()
            self.baskets = []

        async def basket(self, legs, reason=None):
            self.baskets.append((legs, reason))
            results = []
            for leg in legs:
                method = self.buy if leg["side"] == "BUY" else self.sell
                await method(
                    leg["quantity"], reason, leg["symbol"], leg.get("product"),
                    leg.get("option_price_guard"),
                )
                results.append(SimpleNamespace(outcome="placed", message=None))
            return results

    ctx = BasketContext()
    ctx.quotes.update({
        "NSE:NIFTY26N2425000CE": 210,
        "NSE:NIFTY26N2425000PE": 205,
        "NSE:NIFTY26N2424200PE": 20,
    })
    legs = SyntheticWeeklyOptionLegs(ctx)
    legs.set_desired(1)

    async def enter_and_exit():
        await legs.advance(AT, "initial LONG")  # hedge only
        assert not ctx.baskets
        await legs.advance(AT, "initial LONG")  # two ATM legs in one basket
        legs.set_desired(0)
        await legs.advance(AT, "exit synthetic")

    asyncio.run(enter_and_exit())

    assert ctx.orders[0] == (
        "BUY", 65, "NSE:NIFTY26N2424200PE", Product.DELIVERY,
    )
    entry_legs = ctx.baskets[0][0]
    assert [(leg["side"], leg["symbol"]) for leg in entry_legs] == [
        ("BUY", "NSE:NIFTY26N2425000CE"),
        ("SELL", "NSE:NIFTY26N2425000PE"),
    ]
    exit_legs = ctx.baskets[1][0]
    assert [(leg["side"], leg["symbol"]) for leg in exit_legs] == [
        ("BUY", "NSE:NIFTY26N2425000PE"),
        ("SELL", "NSE:NIFTY26N2424200PE"),
        ("SELL", "NSE:NIFTY26N2425000CE"),
    ]
    assert ctx.positions == {}


def test_13l_synthetic_reversal_closes_old_pair_and_hedge_before_new_atm_pair():
    ctx = OptionSellerContext()
    ctx.quotes.update({
        "NSE:NIFTY26N2425000CE": 210,
        "NSE:NIFTY26N2425000PE": 205,
        "NSE:NIFTY26N2424200PE": 20,
        "NSE:NIFTY26N2425800CE": 20,
    })
    legs = SyntheticWeeklyOptionLegs(ctx)
    legs.set_desired(1)

    async def reverse():
        for _ in range(3):
            await legs.advance(AT, "initial LONG")
        legs.set_desired(-1)
        for _ in range(6):
            await legs.advance(AT, "reverse SHORT")

    asyncio.run(reverse())

    assert ctx.orders[3:6] == [
        ("BUY", 65, "NSE:NIFTY26N2425000PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2424200PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
    ]
    assert ctx.orders[-3:] == [
        ("BUY", 65, "NSE:NIFTY26N2425800CE", Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26N2425000PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2425000CE", Product.DELIVERY),
    ]
    assert legs.position == -65


def test_13l_synthetic_margin_rejection_replaces_800_with_600_point_hedge():
    atm_put = "NSE:NIFTY26N2425000PE"

    class MarginRetryContext(OptionSellerContext):
        def __init__(self):
            super().__init__()
            self.rejected_once = False

        async def sell(self, quantity=None, reason=None, symbol=None, product=None,
                       option_price_guard=None):
            if symbol == atm_put and not self.rejected_once:
                self.rejected_once = True
                self.orders.append(("SELL", quantity, symbol, product))
                return SimpleNamespace(
                    outcome="rejected", message="insufficient margin shortfall",
                )
            return await super().sell(
                quantity, reason, symbol, product, option_price_guard,
            )

    ctx = MarginRetryContext()
    ctx.quotes.update({
        "NSE:NIFTY26N2425000CE": 210,
        atm_put: 205,
        "NSE:NIFTY26N2424200PE": 20,
        "NSE:NIFTY26N2424400PE": 30,
    })
    legs = SyntheticWeeklyOptionLegs(ctx)
    legs.set_desired(1)

    async def enter_with_retry():
        for _ in range(7):
            await legs.advance(AT, "initial LONG")

    asyncio.run(enter_with_retry())

    assert ("SELL", 65, "NSE:NIFTY26N2424200PE", Product.DELIVERY) in ctx.orders
    assert ("BUY", 65, "NSE:NIFTY26N2424400PE", Product.DELIVERY) in ctx.orders
    assert ctx.orders[-1] == ("SELL", 65, atm_put, Product.DELIVERY)
    assert legs.hedge_points == 600
    assert legs.position == 65


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
    legs._session_open = lambda _now: True

    asyncio.run(legs.buy(reason="initial LONG", product=Product.INTRADAY))

    assert ctx.orders
    assert all(order[3] == Product.DELIVERY for order in ctx.orders)


def test_option_wrapper_never_orders_before_0915_from_ticks_or_clock():
    ctx = OptionSellerContext()
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller(
        NiftyTrailingReversalOptimizedITMOptionSeller.Params(itm_points=900), ctx, 300,
    )
    pre_open = datetime(2026, 11, 20, 9, 14, 59, tzinfo=IST)

    async def wait_for_open():
        await strategy.on_start()
        assert isinstance(strategy.ctx, ShortWeeklyOptionLegs)
        strategy.ctx.set_desired(1)
        await strategy.on_tick(Tick("NSE:NIFTY50-INDEX", 25_000, pre_open))
        await strategy.on_clock(pre_open)
        assert not ctx.orders

        await strategy.on_tick(Tick(
            "NSE:NIFTY50-INDEX", 25_000, pre_open + timedelta(seconds=1),
        ))

    asyncio.run(wait_for_open())

    assert ctx.orders == [
        ("BUY", 65, "NSE:NIFTY26N2424000PE", Product.DELIVERY),
        ("SELL", 65, "NSE:NIFTY26N2425900PE", Product.DELIVERY),
    ]


@pytest.mark.parametrize(
    "outside_session",
    [
        datetime(2026, 11, 20, 8, 0, tzinfo=IST),
        datetime(2026, 11, 20, 15, 30, tzinfo=IST),
        datetime(2026, 11, 21, 10, 0, tzinfo=IST),
    ],
)
def test_option_adapter_does_not_start_transitions_outside_regular_session(outside_session):
    ctx = OptionSellerContext()
    ctx.quotes["NSE:NIFTY26N2425900PE"] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(outside_session))

    assert not ctx.orders


def test_new_entry_after_1510_skips_the_expiring_contract():
    expiry = date(2026, 11, 17)
    next_expiry = date(2026, 11, 24)
    expiring = "NSE:NIFTY26N1725900PE"
    replacement = "NSE:NIFTY26N2425900PE"
    replacement_hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext()
    ctx.contracts = [
        OptionContract(f"NSE:NIFTY26N17{strike}PE", "PE", float(strike), expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ] + [
        OptionContract(f"NSE:NIFTY26N24{strike}PE", "PE", float(strike), next_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    ctx.quotes[expiring] = 950
    ctx.quotes[replacement] = 960
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(datetime(2026, 11, 17, 15, 11, tzinfo=IST)))

    assert ctx.orders == [
        ("BUY", 65, replacement_hedge, Product.DELIVERY),
        ("SELL", 65, replacement, Product.DELIVERY),
    ]
    assert ctx.positions == {replacement: -65, replacement_hedge: 65}
    assert legs.frozen is not None and legs.frozen.expiry == next_expiry


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


def test_13l_selection_defers_when_only_non_100_short_and_hedge_strikes_are_listed():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    ctx = OptionSellerContext()
    ctx.last_price = 25_050
    ctx.contracts = [OptionContract(
        "NSE:NIFTY26N2425950PE", "PE", 25_950, EXPIRY, 65,
    ), OptionContract(
        "NSE:NIFTY26N2424050PE", "PE", 24_050, EXPIRY, 65,
    )]
    ctx.quotes["NSE:NIFTY26N2425950PE"] = 950
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert not ctx.orders
    assert any("no current-week PE" in message for message in ctx.logs)


def test_13l_uses_next_deeper_itm_100_strike_when_target_is_between_strikes():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    pe_ctx = OptionSellerContext()
    pe_ctx.last_price = 25_050  # 25,950 PE target rounds deeper ITM to 26,000 PE.
    pe_ctx.contracts.append(OptionContract(
        "NSE:NIFTY26N2425950PE", "PE", 25_950, EXPIRY, 65,
    ))
    pe_ctx.quotes["NSE:NIFTY26N2426000PE"] = 950
    pe_ctx.quotes["NSE:NIFTY26N2424200PE"] = 50
    pe_legs = StrictHundredStrikeWeeklyOptionLegs(pe_ctx, 900)
    pe_legs.set_desired(1)

    ce_ctx = OptionSellerContext()
    ce_ctx.last_price = 25_050  # 24,150 CE target rounds deeper ITM to 24,100 CE.
    ce_ctx.contracts.append(OptionContract(
        "NSE:NIFTY26N2424150CE", "CE", 24_150, EXPIRY, 65,
    ))
    ce_ctx.quotes["NSE:NIFTY26N2424100CE"] = 950
    ce_ctx.quotes["NSE:NIFTY26N2425800CE"] = 50
    ce_legs = StrictHundredStrikeWeeklyOptionLegs(ce_ctx, 900)
    ce_legs.set_desired(-1)

    asyncio.run(pe_legs.advance(AT, "initial LONG"))
    asyncio.run(ce_legs.advance(AT, "initial SHORT"))

    assert pe_legs.hedge_points == ce_legs.hedge_points == 800
    assert pe_ctx.orders[0] == (
        "BUY", 65, "NSE:NIFTY26N2424200PE", Product.DELIVERY,
    )
    assert ce_ctx.orders[0] == (
        "BUY", 65, "NSE:NIFTY26N2425800CE", Product.DELIVERY,
    )
    assert pe_ctx.orders[-1] == (
        "SELL", 65, "NSE:NIFTY26N2426000PE", Product.DELIVERY,
    )
    assert ce_ctx.orders[-1] == (
        "SELL", 65, "NSE:NIFTY26N2424100CE", Product.DELIVERY,
    )


def test_13l_scans_1000_900_800_itm_and_submits_full_quantity_at_valid_prices():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    class DepthContext(OptionSellerContext):
        async def executable_quote(self, symbol, side):
            books = {
                # The opening 1000-ITM book is both below its intrinsic floor and too wide.
                "NSE:NIFTY26N2426000PE": (900, 1_000),
                "NSE:NIFTY26N2425900PE": (850, 950),
                "NSE:NIFTY26N2425800PE": (805, 810),
                "NSE:NIFTY26N2424200PE": (45, 50),
            }
            book = books.get(symbol)
            if book is None:
                return None
            bid, ask = book
            return QuoteSnapshot(
                bid if side == "SELL" else ask, AT, "depth", bid=bid, ask=ask,
            )

    ctx = DepthContext(quantity=130)
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert ctx.orders == [
        ("BUY", 130, "NSE:NIFTY26N2424200PE", Product.DELIVERY),
        ("SELL", 130, "NSE:NIFTY26N2425800PE", Product.DELIVERY),
    ]
    assert ctx.price_guards[0] == (
        "BUY", "NSE:NIFTY26N2424200PE", pytest.approx(55),
    )
    assert ctx.price_guards[1][:2] == ("SELL", "NSE:NIFTY26N2425800PE")
    assert ctx.price_guards[1][2] == pytest.approx(795.76)
    assert any("1000-ITM" in message and "skipped" in message for message in ctx.logs)
    assert any("900-ITM" in message and "skipped" in message for message in ctx.logs)
    assert any("800-ITM" in message and "selected" in message for message in ctx.logs)


@pytest.mark.parametrize(
    ("dte", "expected_floor"),
    [
        (0, 931.00), (1, 931.00), (2, 931.00), (3, 931.00),
        (4, 933.94), (5, 936.88), (6, 939.82), (7, 942.76), (8, 945.70),
    ],
)
def test_13l_fair_short_floor_adds_three_rupees_per_dte(dte, expected_floor):
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    ctx = OptionSellerContext()
    ctx.last_price = 25_859
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    contract = OptionContract(
        "NSE:NIFTY26800PE", "PE", 26_800, AT.date() + timedelta(days=dte), 65,
    )
    quote = QuoteSnapshot(2_000, AT, "test")

    floor, problem = legs._entry_price_guard(contract, quote, "SELL")

    assert problem is None
    assert floor == pytest.approx(expected_floor)


def test_13l_rejects_overpriced_hedge_before_opening_short():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    class BadHedgeContext(OptionSellerContext):
        async def executable_quote(self, symbol, side):
            if symbol == "NSE:NIFTY26N2426000PE":
                bid, ask = 1_005, 1_010
            elif symbol == "NSE:NIFTY26N2424200PE":
                bid, ask = 1, 100
            else:
                return None
            return QuoteSnapshot(
                bid if side == "SELL" else ask, AT, "depth", bid=bid, ask=ask,
            )

    ctx = BadHedgeContext(quantity=130)
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "initial LONG"))

    assert not ctx.orders
    assert any("invalid hedge price" in message for message in ctx.logs)


def test_13l_exits_holiday_expiry_on_previous_nse_session_at_1510():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    short = "NSE:NIFTY26N2425900PE"  # Nominal 24 Nov 2026 expiry is an NSE holiday.
    hedge = "NSE:NIFTY26N2424200PE"
    next_expiry = date(2026, 12, 1)
    replacement = "NSE:NIFTY26D0125900PE"
    replacement_hedge = "NSE:NIFTY26D0124200PE"
    ctx = OptionSellerContext({short: -65, hedge: 65})
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26D01{strike}PE", "PE", float(strike), next_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    ctx.quotes[replacement] = 950
    ctx.quotes[replacement_hedge] = 50
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    async def roll():
        at = datetime(2026, 11, 23, 15, 10, tzinfo=IST)
        await legs.advance(at.replace(minute=9))
        assert not ctx.orders

        await legs.advance(at)
        assert ctx.orders == [("BUY", 65, short, Product.DELIVERY)]
        assert legs.roll_after == at.date()
        assert legs.roll_expiry_after == date(2026, 11, 25)

        await legs.advance(at)
        await legs.advance(at)

    asyncio.run(roll())

    assert ctx.orders == [
        ("BUY", 65, short, Product.DELIVERY),
        ("BUY", 65, replacement_hedge, Product.DELIVERY),
        ("SELL", 65, replacement, Product.DELIVERY),
        ("SELL", 65, hedge, Product.DELIVERY),
    ]
    assert ctx.positions == {replacement: -65, replacement_hedge: 65}
    assert legs.roll_after is None
    assert legs.roll_expiry_after is None
    assert legs.frozen is not None
    assert legs.frozen.dte == 8
    assert legs.frozen.expiry == next_expiry


def test_13l_new_entry_after_holiday_expiry_cutoff_skips_expiring_contract():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    nominal_holiday_expiry = date(2026, 11, 24)
    next_expiry = date(2026, 12, 1)
    expiring = "NSE:NIFTY26N2425900PE"
    replacement = "NSE:NIFTY26D0125900PE"
    replacement_hedge = "NSE:NIFTY26D0124200PE"
    ctx = OptionSellerContext()
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26D01{strike}PE", "PE", float(strike), next_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    assert any(contract.symbol == expiring and contract.expiry == nominal_holiday_expiry
               for contract in ctx.contracts)
    ctx.quotes[expiring] = 950
    ctx.quotes[replacement] = 960
    ctx.quotes[replacement_hedge] = 50
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(datetime(2026, 11, 23, 15, 11, tzinfo=IST)))

    assert ctx.orders == [
        ("BUY", 65, replacement_hedge, Product.DELIVERY),
        ("SELL", 65, replacement, Product.DELIVERY),
    ]
    assert ctx.positions == {replacement: -65, replacement_hedge: 65}
    assert legs.frozen is not None and legs.frozen.expiry == next_expiry


def test_13l_does_not_exit_a_normal_expiry_on_the_previous_session():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    normal_expiry = date(2026, 11, 17)
    short = "NSE:NIFTY26N1725900PE"
    hedge = "NSE:NIFTY26N1724000PE"
    ctx = OptionSellerContext({short: -65, hedge: 65})
    ctx.contracts = [
        OptionContract(short, "PE", 25_900, normal_expiry, 65),
        OptionContract(hedge, "PE", 24_000, normal_expiry, 65),
    ]
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    asyncio.run(legs.advance(datetime(2026, 11, 16, 15, 10, tzinfo=IST)))

    assert not ctx.orders
    assert ctx.positions == {short: -65, hedge: 65}


def test_13l_rolls_into_next_week_in_same_session_after_expiry_cover():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    expiry = date(2026, 11, 17)
    next_expiry = date(2026, 11, 24)
    at = datetime(2026, 11, 17, 15, 10, tzinfo=IST)
    old = "NSE:NIFTY26N1725900PE"
    old_hedge = "NSE:NIFTY26N1724000PE"
    new = "NSE:NIFTY26N2425900PE"
    new_hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({old: -65, old_hedge: 65})
    ctx.contracts = [
        OptionContract(old, "PE", 25_900, expiry, 65),
        OptionContract(old_hedge, "PE", 24_000, expiry, 65),
        OptionContract(new, "PE", 25_900, next_expiry, 65),
        OptionContract(new_hedge, "PE", 24_000, next_expiry, 65),
    ]
    ctx.quotes[new] = 950
    ctx.quotes[new_hedge] = 50
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = old_hedge
    legs.set_desired(1)

    async def roll():
        await legs.monitor_expiry(at)
        assert ctx.positions == {old_hedge: 65}
        assert legs.roll_after == expiry
        assert legs.roll_expiry_after == expiry + timedelta(days=1)
        assert ctx.strategy_state[legs.state_key]["roll_after"] == "2026-11-17"
        assert ctx.strategy_state[legs.state_key]["roll_expiry_after"] == "2026-11-18"
        await legs.advance(at)
        await legs.advance(at)

    asyncio.run(roll())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("BUY", 65, new_hedge, Product.DELIVERY),
        ("SELL", 65, new, Product.DELIVERY),
        ("SELL", 65, old_hedge, Product.DELIVERY),
    ]
    assert ctx.positions == {new: -65, new_hedge: 65}
    assert legs.roll_after is None
    assert legs.roll_expiry_after is None
    assert legs.frozen is not None and legs.frozen.expiry == next_expiry

    # A reversal later in the same session must stay on the newly rolled expiry rather than
    # selecting the just-expired contract, which can remain visible in the broker chain.
    next_call = "NSE:NIFTY26N2424100CE"
    next_call_hedge = "NSE:NIFTY26N2426000CE"
    ctx.contracts += [
        OptionContract(next_call, "CE", 24_100, next_expiry, 65),
        OptionContract(next_call_hedge, "CE", 26_000, next_expiry, 65),
    ]
    ctx.quotes[next_call] = 950
    ctx.quotes[next_call_hedge] = 50
    legs.set_desired(-1)

    async def reverse_after_roll():
        later = at + timedelta(minutes=10)
        await legs.advance(later)
        await legs.advance(later)
        await legs.advance(later)

    asyncio.run(reverse_after_roll())

    assert ctx.orders[-4:] == [
        ("BUY", 65, new, Product.DELIVERY),
        ("BUY", 65, next_call_hedge, Product.DELIVERY),
        ("SELL", 65, next_call, Product.DELIVERY),
        ("SELL", 65, new_hedge, Product.DELIVERY),
    ]
    assert ctx.positions == {next_call: -65, next_call_hedge: 65}


def test_13l_same_session_roll_requires_current_quote_at_1510():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    at = datetime(2026, 11, 17, 15, 10, tzinfo=IST)
    next_expiry = date(2026, 11, 24)
    short = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"

    class CurrentQuoteContext(OptionSellerContext):
        observed_at = at - timedelta(seconds=3)

        async def executable_quote(self, symbol, side):
            return QuoteSnapshot(950, self.observed_at, "current-roll-depth")

    ctx = CurrentQuoteContext()
    ctx.contracts = [
        OptionContract(short, "PE", 25_900, next_expiry, 65),
        OptionContract(hedge, "PE", 24_000, next_expiry, 65),
    ]
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.desired_direction = 1
    legs.roll_after = at.date()
    legs.roll_expiry_after = at.date() + timedelta(days=1)

    asyncio.run(legs.advance(at, "expiry roll"))
    assert not ctx.orders

    ctx.observed_at = at
    asyncio.run(legs.advance(at, "expiry roll"))

    assert ctx.orders == [
        ("BUY", 65, hedge, Product.DELIVERY),
        ("SELL", 65, short, Product.DELIVERY),
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


@pytest.mark.parametrize("margin_rejections", [2, 3])
def test_margin_rejection_retries_1000_then_800_then_600_otm_and_stops(margin_rejections):
    short = "NSE:NIFTY26N2425900PE"

    class MarginRetryContext(OptionSellerContext):
        short_attempts = 0

        async def sell(self, quantity=None, reason=None, symbol=None, product=None):
            if symbol == short:
                self.short_attempts += 1
                if self.short_attempts <= margin_rejections:
                    self.orders.append(("SELL", quantity, symbol, product))
                    return SimpleNamespace(
                        outcome="rejected",
                        message="RED:Margin Shortfall:INR 3,208.27&#x20;",
                    )
            return await super().sell(quantity, reason, symbol, product)

    ctx = MarginRetryContext()
    ctx.quotes[short] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.set_desired(1)

    asyncio.run(legs.advance(AT, "option transition"))
    assert legs.hedge_points == 800
    legs = ShortWeeklyOptionLegs(ctx, 900)  # fallback survives a process/adapter restart
    legs.set_desired(1)
    assert legs.hedge_points == 800
    asyncio.run(legs.advance(AT, "option transition"))
    assert legs.hedge_points == 600
    asyncio.run(legs.advance(AT, "option transition"))

    hedge_buys = [symbol for side, _, symbol, _ in ctx.orders if side == "BUY"]
    assert hedge_buys == [
        "NSE:NIFTY26N2424000PE",
        "NSE:NIFTY26N2424200PE",
        "NSE:NIFTY26N2424400PE",
    ]
    assert legs.hedge_points == 600

    if margin_rejections == 2:
        assert ctx.positions[short] == -65
        assert not legs.margin_retry_exhausted
    else:
        assert short not in ctx.positions
        assert legs.margin_retry_exhausted
        order_count = len(ctx.orders)
        legs = ShortWeeklyOptionLegs(ctx, 900)
        legs.set_desired(1)
        asyncio.run(legs.advance(AT, "option transition"))
        assert len(ctx.orders) == order_count
        assert any("600-point safety limit" in message for message in ctx.logs)


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


def test_13l_overnight_gap_below_700_resets_from_first_0915_clock_check():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    opened = datetime(2026, 11, 19, 15, 20, tzinfo=IST)
    next_open = datetime(2026, 11, 20, 9, 15, tzinfo=IST)
    old = "NSE:NIFTY26N2425900PE"
    old_hedge = "NSE:NIFTY26N2424200PE"
    replacement = "NSE:NIFTY26N2426400PE"
    ctx = OptionSellerContext({old: -65, old_hedge: 65})
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = old_hedge
    legs.desired_direction = 1
    # The entry sold above the frozen ₹700 level, so the reset carries overnight armed.
    legs.frozen = legs._classify_reset("PE", 25_900, 25_000, 900, EXPIRY, opened)
    assert legs.frozen.reset_level == 700
    legs.last_premium = 900
    legs.reset_armed = True

    async def gap_and_reset():
        # NIFTY gaps up 500 points. The current ask of the held PE gaps below ₹700.
        ctx.last_price = 25_500
        ctx.quotes[old] = 699
        ctx.quotes[replacement] = 950
        await legs.monitor_reset(next_open)
        await legs.advance(next_open)
        await legs.advance(next_open)

    asyncio.run(gap_and_reset())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("SELL", 65, replacement, Product.DELIVERY),
    ]
    assert ctx.positions == {replacement: -65, old_hedge: 65}
    assert legs.hedge_symbol == old_hedge
    assert any("reusing existing margin hedge" in message for message in ctx.logs)
    assert not legs.reset_pending


def test_13l_gap_reset_keeps_old_short_when_required_hedge_price_is_invalid():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    old = "NSE:NIFTY26N2425900PE"
    old_hedge = "NSE:NIFTY26N2424200PE"
    replacement = "NSE:NIFTY26N2426500PE"
    replacement_hedge = "NSE:NIFTY26N2424700PE"
    at = datetime(2026, 11, 20, 9, 15, tzinfo=IST)

    class GapDepthContext(OptionSellerContext):
        async def executable_quote(self, symbol, side):
            books = {
                old: (690, 699),
                replacement: (1_005, 1_010),
                # A valid replacement short must not cause the old leg to be covered when the
                # new hedge would require paying through an abnormal book.
                replacement_hedge: (1, 100),
            }
            book = books.get(symbol)
            if book is None:
                return None
            bid, ask = book
            return QuoteSnapshot(
                bid if side == "SELL" else ask, at, "depth", bid=bid, ask=ask,
            )

    # The held hedge is undersized, so the reset genuinely requires buying the newly selected
    # hedge and must remain pending when that ask is invalid.
    ctx = GapDepthContext({old: -130, old_hedge: 65}, quantity=130)
    ctx.last_price = 25_500
    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = old_hedge
    legs.desired_direction = 1
    legs.frozen = legs._classify_reset("PE", 25_900, 25_000, 900, EXPIRY, AT)
    legs.reset_armed = True

    asyncio.run(legs.monitor_reset(at))

    assert legs.reset_pending
    assert legs.replace_symbol is None
    assert not ctx.orders
    assert ctx.positions == {old: -130, old_hedge: 65}
    assert any("reset deferred: invalid hedge price" in message for message in ctx.logs)


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


def test_expiry_cover_does_not_wait_for_next_week_quote_then_reopens_and_reclassifies():
    expiry_at = datetime(2026, 11, 24, 15, 10, tzinfo=IST)
    old = "NSE:NIFTY26N2425900PE"
    old_hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({old: -65, old_hedge: 65})
    next_expiry = date(2026, 12, 1)
    ctx.contracts += [
        OptionContract(f"NSE:NIFTY26D01{strike}PE", "PE", float(strike), next_expiry, 65)
        for strike in range(24_000, 26_501, 100)
    ]
    gap_candidate = "NSE:NIFTY26D0126100PE"
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = old_hedge
    legs.set_desired(1)

    async def roll():
        # There is deliberately no executable next-week short quote at 15:10. The expiring
        # risk must still be covered, with reopening deferred to the next session.
        await legs.monitor_expiry(expiry_at)
        assert ctx.positions == {old_hedge: 65}
        assert legs.roll_after == date(2026, 11, 25)
        assert ctx.strategy_state[legs.state_key]["roll_after"] == "2026-11-25"
        await legs.advance(expiry_at)
        assert ctx.positions == {}
        # Reselect from the next session's spot rather than carrying the 25,900 strike selected
        # as the pre-close safety candidate through this 200-point overnight gap.
        ctx.last_price = 25_200
        ctx.quotes[gap_candidate] = 960
        await legs.advance(datetime(2026, 11, 25, 9, 15, tzinfo=IST))

    asyncio.run(roll())

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
        ("SELL", 65, old_hedge, Product.DELIVERY),
        ("BUY", 65, "NSE:NIFTY26D0124200PE", Product.DELIVERY),
        ("SELL", 65, gap_candidate, Product.DELIVERY),
    ]
    assert legs.frozen is not None and legs.frozen.classified_at.date() == date(2026, 11, 25)


def test_restart_after_expiry_still_covers_the_stranded_old_contract():
    old = "NSE:NIFTY26N2425900PE"
    old_hedge = "NSE:NIFTY26N2424000PE"
    after_expiry = datetime(2026, 11, 25, 9, 16, tzinfo=IST)
    ctx = OptionSellerContext({old: -65, old_hedge: 65})
    ctx.strategy_state = {
        ShortWeeklyOptionLegs.state_key: {
            "frozen": classify_reset("PE", 25_900, 25_000, 950, EXPIRY, AT).as_dict(),
            "last_premium": 900,
            "reset_armed": True,
            "reset_pending": False,
            "roll_after": None,
            "pending_audit": None,
            "hedge_symbol": old_hedge,
        },
    }

    legs = ShortWeeklyOptionLegs(ctx, 900)
    asyncio.run(legs.advance(after_expiry))

    assert ctx.orders == [
        ("BUY", 65, old, Product.DELIVERY),
    ]
    assert ctx.positions == {old_hedge: 65}
    assert legs.roll_after == after_expiry.date()
    assert ctx.strategy_state[legs.state_key]["roll_after"] == "2026-11-25"


def test_size_reduction_covers_short_before_trimming_surplus_margin_hedge():
    short = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({short: -130, hedge: 130}, quantity=65)
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    async def resize():
        await legs.advance(AT)
        assert ctx.positions == {short: -65, hedge: 130}
        await legs.advance(AT)

    asyncio.run(resize())

    assert ctx.orders == [
        ("BUY", 65, short, Product.DELIVERY),
        ("SELL", 65, hedge, Product.DELIVERY),
    ]
    assert ctx.positions == {short: -65, hedge: 65}


def test_partial_short_entry_keeps_full_hedge_before_completing_short():
    short = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({short: -30, hedge: 65}, quantity=65)
    ctx.quotes[short] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    asyncio.run(legs.advance(AT))

    assert ctx.orders == [
        ("SELL", 35, short, Product.DELIVERY),
    ]
    assert ctx.positions == {short: -65, hedge: 65}


def test_size_increase_expands_hedge_before_adding_to_short():
    short = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({short: -65, hedge: 65}, quantity=130)
    ctx.quotes[short] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    asyncio.run(legs.advance(AT))

    assert ctx.orders == [
        ("BUY", 65, hedge, Product.DELIVERY),
        ("SELL", 65, short, Product.DELIVERY),
    ]
    assert ctx.positions == {short: -130, hedge: 130}


def test_live_size_edit_does_not_change_target_mid_reconciliation_pass():
    short = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"

    class SizeEditContext(OptionSellerContext):
        async def save_strategy_state(self, state):
            await super().save_strategy_state(state)
            self.quantity = 130

    ctx = SizeEditContext({short: -65, hedge: 65}, quantity=65)
    ctx.quotes[short] = 950
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    async def resize():
        await legs.advance(AT)
        assert ctx.orders == []
        assert ctx.positions == {short: -65, hedge: 65}
        await legs.advance(AT)

    asyncio.run(resize())

    assert ctx.orders == [
        ("BUY", 65, hedge, Product.DELIVERY),
        ("SELL", 65, short, Product.DELIVERY),
    ]
    assert ctx.positions == {short: -130, hedge: 130}


def test_duplicate_same_direction_shorts_are_reduced_to_one_contract():
    duplicate = "NSE:NIFTY26N2425800PE"
    wanted = "NSE:NIFTY26N2425900PE"
    hedge = "NSE:NIFTY26N2424000PE"
    ctx = OptionSellerContext({duplicate: -65, wanted: -65, hedge: 65})
    legs = ShortWeeklyOptionLegs(ctx, 900)
    legs.hedge_symbol = hedge
    legs.set_desired(1)

    asyncio.run(legs.advance(AT))

    assert ctx.orders == [
        ("BUY", 65, duplicate, Product.DELIVERY),
    ]
    assert ctx.positions == {wanted: -65, hedge: 65}
    assert any("duplicate short option exposure" in message for message in ctx.logs)


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


def test_strategy_exposes_only_its_valid_five_minute_timeframe():
    strategy = NiftyTrailingReversalOptimizedITMOptionSeller

    assert strategy.timeframes == ("5m",)
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


def test_13l_uses_flat_700_reset_for_every_dte_and_iv_regime():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    legs = StrictHundredStrikeWeeklyOptionLegs(OptionSellerContext(), 900)

    zero_dte = legs._classify_reset(
        "PE", 25_900, 25_000, 900, AT.date(), AT,
    )
    low_2dte = legs._classify_reset(
        "PE", 25_900, 25_000, 900, AT.date() + timedelta(days=2), AT,
    )
    low_4dte = legs._classify_reset(
        "PE", 25_900, 25_000, 900, AT.date() + timedelta(days=4), AT,
    )
    high_4dte = legs._classify_reset(
        "PE", 25_900, 25_000, 1_200, AT.date() + timedelta(days=4), AT,
    )

    assert zero_dte.reset_level == 700
    assert (low_2dte.iv_regime, low_2dte.reset_level) == ("LOW", 700)
    assert (low_4dte.iv_regime, low_4dte.reset_level) == ("LOW", 700)
    assert (high_4dte.iv_regime, high_4dte.reset_level) == ("HIGH", 700)


def test_13l_migrates_an_open_legacy_reset_checkpoint_to_700():
    from app.strategies.builtin.nifty_adaptive_premium_reset_13l import (
        StrictHundredStrikeWeeklyOptionLegs,
    )

    ctx = OptionSellerContext()
    legacy = classify_reset("PE", 25_900, 25_000, 900, EXPIRY, AT)
    ctx.strategy_state = {
        ShortWeeklyOptionLegs.state_key: {
            "frozen": {**legacy.as_dict(), "reset_level": 800},
        },
    }

    legs = StrictHundredStrikeWeeklyOptionLegs(ctx, 900)

    assert legs.frozen is not None
    assert legs.frozen.reset_level == 700


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
