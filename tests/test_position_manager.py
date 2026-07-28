from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from src.models import OptionLeg, Quote, RatioSpread
from src.position_manager import PositionManager


class FakeExchange:
    def __init__(self, quotes: dict[int, Quote]) -> None:
        self._quotes = quotes
        self.parsed_positions = []

    async def parse_option_positions(self):
        return self.parsed_positions

    async def get_best_quote(self, product_id: int) -> Quote:
        return self._quotes[product_id]


@pytest.mark.asyncio
async def test_detect_ratio_spreads_returns_call_and_put() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)

    call_long = OptionLeg(1, "BTC-1C", "call", 100.0, now, 1.0, 101.0)
    call_short = OptionLeg(2, "BTC-2C", "call", 100.0, now, -2.0, 102.0)
    put_long = OptionLeg(3, "BTC-1P", "put", 95.0, now, 1.0, 96.0)
    put_short = OptionLeg(4, "BTC-2P", "put", 95.0, now, -2.0, 97.0)

    fake_exchange = FakeExchange(
        quotes={
            1: Quote(best_bid=101.5, best_ask=102.5),
            2: Quote(best_bid=100.5, best_ask=101.5),
            3: Quote(best_bid=96.5, best_ask=97.5),
            4: Quote(best_bid=94.5, best_ask=95.5),
        }
    )
    fake_exchange.parsed_positions = [call_long, call_short, put_long, put_short]

    manager = PositionManager(fake_exchange)
    spreads = await manager.detect_ratio_spreads([2.0])

    assert len(spreads) == 2
    assert {spread.long_leg.option_type for spread in spreads} == {"call", "put"}
    assert all(spread.ratio == 2.0 for spread in spreads)


@pytest.mark.asyncio
async def test_compute_unrealized_pnl_uses_best_bid_and_ask() -> None:
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    long_leg = OptionLeg(10, "BTC-LONG", "call", 100.0, now, 1.0, 95.0)
    short_leg = OptionLeg(11, "BTC-SHORT", "call", 100.0, now, -2.0, 96.0)

    ratio = RatioSpread(
        long_leg=long_leg,
        short_leg=short_leg,
        short_leg_each_qty=1.0,
        ratio=2.0,
    )

    fake_exchange = FakeExchange(
        quotes={
            10: Quote(best_bid=106.0, best_ask=107.0),
            11: Quote(best_bid=98.0, best_ask=99.0),
        }
    )

    manager = PositionManager(fake_exchange)
    pnl = await manager.compute_unrealized_pnl(ratio)

    # Long pnl = (106 - 95) * 1 = 11
    # Short pnl = (96 - 99) * 2 = -6
    assert pnl == pytest.approx(5.0)
