from __future__ import annotations

import math

from src.exchange_client import DeltaExchangeClient
from src.models import OptionLeg, RatioSpread


class PositionManager:
    def __init__(self, exchange: DeltaExchangeClient) -> None:
        self.exchange = exchange

    async def detect_ratio_spreads(self, accepted_ratios: list[float]) -> list[RatioSpread]:
        """
        Detect all call and put ratio spreads simultaneously.
        Returns a list of RatioSpread objects (both call and put, if they exist).
        """
        legs = await self.exchange.parse_option_positions()
        if not legs:
            raise RuntimeError("No open option positions found")

        if not accepted_ratios:
            raise ValueError("At least one ratio must be configured for ratio spread detection.")

        # Separate by option type
        call_legs = [x for x in legs if x.option_type == "call"]
        put_legs = [x for x in legs if x.option_type == "put"]

        found_spreads: list[RatioSpread] = []

        # Detect call ratio spreads
        if call_legs:
            call_ratio = self._find_ratio_spread_for_type(call_legs, accepted_ratios)
            if call_ratio is not None:
                found_spreads.append(call_ratio)

        # Detect put ratio spreads
        if put_legs:
            put_ratio = self._find_ratio_spread_for_type(put_legs, accepted_ratios)
            if put_ratio is not None:
                found_spreads.append(put_ratio)

        if not found_spreads:
            leg_summary = ", ".join(
                f"pid={x.product_id}|{x.option_type}|strike={x.strike}|exp={x.expiry.isoformat()}|size={x.size}"
                for x in legs
            )
            expected_summary = ", ".join(str(r) for r in accepted_ratios)
            raise RuntimeError(
                "No valid ratio spread found for ratios: "
                f"{expected_summary}. Parsed legs: "
                + leg_summary
            )

        return found_spreads

    def _find_ratio_spread_for_type(self, legs: list[OptionLeg], accepted_ratios: list[float]) -> RatioSpread | None:
        """
        Find a single ratio spread within legs of the same option type.
        Returns the first matching ratio spread or None if none found.
        """
        longs = [x for x in legs if x.size > 0]
        shorts = [x for x in legs if x.size < 0]

        for long_leg in longs:
            for short_leg in shorts:
                same_expiry = (
                    long_leg.expiry == short_leg.expiry
                    or long_leg.expiry.date() == short_leg.expiry.date()
                )
                if not same_expiry:
                    continue

                long_qty = abs(long_leg.size)
                short_qty = abs(short_leg.size)
                if long_qty <= 0:
                    continue

                actual_ratio = short_qty / long_qty
                for expected_ratio in accepted_ratios:
                    if math.isclose(
                        actual_ratio,
                        expected_ratio,
                        rel_tol=2e-2,
                        abs_tol=1e-8,
                    ):
                        return RatioSpread(
                            long_leg=long_leg,
                            short_leg=short_leg,
                            short_leg_each_qty=long_qty,
                            ratio=actual_ratio,
                        )

        return None

    async def compute_unrealized_pnl(self, ratio: RatioSpread) -> float:
        # Calculate PnL based on entry price and actual liquidation prices.
        # Long positions close at best_bid (conservative: worst price when selling)
        # Short positions close at best_ask (conservative: worst price when buying back)
        long_q = await self.exchange.get_best_quote(ratio.long_leg.product_id)
        short_q = await self.exchange.get_best_quote(ratio.short_leg.product_id)

        long_qty = abs(ratio.long_leg.size)
        short_qty = abs(ratio.short_leg.size)

        # Long leg PnL: (best_bid - entry_price) * qty * contract_value
        # (position closed by selling at bid price)
        long_leg_pnl = (
            long_qty
            * ratio.long_leg.contract_value
            * (long_q.best_bid - ratio.long_leg.entry_price)
        )

        # Short leg PnL: (entry_price - best_ask) * qty * contract_value
        # (position closed by buying back at ask price)
        short_leg_pnl = (
            short_qty
            * ratio.short_leg.contract_value
            * (ratio.short_leg.entry_price - short_q.best_ask)
        )

        return long_leg_pnl + short_leg_pnl

