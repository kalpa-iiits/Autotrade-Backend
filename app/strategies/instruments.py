"""Search broker-native NSE stock and index catalogues."""

import re

from app.brokers.base import Instrument


def _normalise(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def search_instruments(instruments: list[Instrument], query: str, limit: int) -> list[Instrument]:
    """Rank exact and prefix ticker matches ahead of company-name matches."""
    needle = _normalise(query)
    if not needle:
        return []

    def score(item: Instrument) -> tuple[int, int, str]:
        ticker = _normalise(item.ticker)
        symbol = _normalise(item.symbol.removeprefix("NSE:"))
        name = _normalise(item.name)
        if needle == ticker or needle == symbol:
            rank = 0
        elif ticker.startswith(needle) or symbol.startswith(needle):
            rank = 1
        elif name.startswith(needle):
            rank = 2
        elif needle in ticker or needle in symbol:
            rank = 3
        else:
            rank = 4
        return rank, len(ticker), ticker

    matches = [item for item in instruments if any(
        needle in _normalise(value) for value in (item.ticker, item.symbol, item.name)
    )]
    return sorted(matches, key=score)[:limit]
