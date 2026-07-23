from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from typing import Optional

from src.config import Settings
from src.exchange_client import DeltaExchangeClient
from src.order_executor import OrderExecutor
from src.position_manager import PositionManager

LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RatioState:
    """Track state for a single ratio spread."""
    ratio_id: str  # "call" or "put"
    status_message: str = "monitoring"
    action: str = "monitoring ratio-spread"
    trigger_pnl: Optional[float] = None


@dataclass(slots=True)
class StrategyState:
    last_index_price: Optional[float] = None
    status_message: str = "starting"
    action: str = "waiting for ratio spreads"
    status: str = "initializing"
    trigger_price: Optional[str] = None
    trigger_pnl: Optional[float] = None
    call_ratio_state: Optional[RatioState] = None
    put_ratio_state: Optional[RatioState] = None


class StrategyEngine:
    def __init__(
        self,
        exchange: DeltaExchangeClient,
        positions: PositionManager,
        executor: OrderExecutor,
        settings: Settings,
    ) -> None:
        self.exchange = exchange
        self.positions = positions
        self.executor = executor
        self.settings = settings
        self.state = StrategyState()

    def _set_strategy_state(
        self,
        action: str,
        status: str,
        status_message: str | None = None,
        trigger_price: str | None = None,
        trigger_pnl: float | None = None,
    ) -> None:
        self.state.action = action
        self.state.status = status
        if status_message is not None:
            self.state.status_message = status_message
        self.state.trigger_price = trigger_price
        self.state.trigger_pnl = trigger_pnl

    async def _close_entire_ratio(self, ratio: RatioSpread) -> None:
        long_qty = abs(ratio.long_leg.size)
        short_qty = abs(ratio.short_leg.size)

        # Long close = sell. Short close = buy.
        filled_long = await self.executor.execute_market_single_submission_with_fill_confirmation(
            product_id=ratio.long_leg.product_id,
            side="sell",
            size=long_qty,
            reduce_only=True,
        )
        filled_short = await self.executor.execute_market_single_submission_with_fill_confirmation(
            product_id=ratio.short_leg.product_id,
            side="buy",
            size=short_qty,
            reduce_only=True,
        )
        LOGGER.info("Ratio close requested long_filled=%s short_filled=%s", filled_long, filled_short)

    def _get_ratio_id(self, ratio: RatioSpread) -> str:
        """Return 'call' or 'put' as the ratio identifier."""
        return ratio.long_leg.option_type

    async def run(self) -> None:
        active_ratios: dict[str, RatioSpread] = {}  # {"call": RatioSpread, "put": RatioSpread}
        wait_cycles = 0
        ratio_scan_task = None

        try:
            while True:
                # If no active ratios, scan for them
                if not active_ratios:
                    self._set_strategy_state(
                        action="waiting for ratio spreads",
                        status="no ratio spreads found",
                        status_message="awaiting configured ratio spreads (call and/or put)",
                        trigger_price=None,
                        trigger_pnl=None,
                    )

                    if ratio_scan_task is None:
                        ratio_scan_task = asyncio.create_task(
                            self.positions.detect_ratio_spreads(self.settings.ratio_spreads)
                        )

                    if ratio_scan_task.done():
                        try:
                            found_ratios = ratio_scan_task.result()
                            ratio_scan_task = None

                            # Build active ratios dict
                            for ratio in found_ratios:
                                ratio_id = self._get_ratio_id(ratio)
                                active_ratios[ratio_id] = ratio
                                LOGGER.info(
                                    "Detected %s ratio spread ratio=%.2f long=%s @%.2f short=%s @%.2f",
                                    ratio_id,
                                    ratio.ratio,
                                    abs(ratio.long_leg.size),
                                    ratio.long_leg.strike,
                                    abs(ratio.short_leg.size),
                                    ratio.short_leg.strike,
                                )

                            # Update state
                            call_status = "detected" if "call" in active_ratios else "not found"
                            put_status = "detected" if "put" in active_ratios else "not found"
                            self._set_strategy_state(
                                action="monitoring ratio-spreads",
                                status="ratio spreads found",
                                status_message=f"call={call_status}, put={put_status}",
                                trigger_price=None,
                                trigger_pnl=None,
                            )
                            if "call" in active_ratios:
                                self.state.call_ratio_state = RatioState("call", "monitoring", "monitoring call ratio")
                            if "put" in active_ratios:
                                self.state.put_ratio_state = RatioState("put", "monitoring", "monitoring put ratio")
                        except Exception as exc:
                            ratio_scan_task = None
                            wait_cycles += 1
                            try:
                                index_price = await self.exchange.get_index_price("BTCUSDT")
                                self.state.last_index_price = index_price
                                LOGGER.info(
                                    "no ratio spread found | BTC index=%.2f | attempt=%s | detail=%s",
                                    index_price,
                                    wait_cycles,
                                    str(exc),
                                )
                            except Exception:
                                LOGGER.info(
                                    "no ratio spread found | BTC index=unavailable | attempt=%s | detail=%s",
                                    wait_cycles,
                                    str(exc),
                                )
                            await asyncio.sleep(self.settings.poll_interval_seconds)
                            continue
                    else:
                        wait_cycles += 1
                        try:
                            index_price = await self.exchange.get_index_price("BTCUSDT")
                            self.state.last_index_price = index_price
                            LOGGER.info(
                                "no ratio spread found | BTC index=%.2f | attempt=%s | status=scan in progress",
                                index_price,
                                wait_cycles,
                            )
                        except Exception:
                            LOGGER.info(
                                "no ratio spread found | BTC index=unavailable | attempt=%s | status=scan in progress",
                                wait_cycles,
                            )
                        await asyncio.sleep(self.settings.poll_interval_seconds)
                        continue

                # Monitor all active ratios
                self._set_strategy_state(
                    action="monitoring ratio-spreads",
                    status="monitoring pnl thresholds",
                    status_message="checking ratio spreads pnl against exit thresholds",
                    trigger_price=None,
                    trigger_pnl=None,
                )

                # Verify positions still exist
                try:
                    current_ratios = await self.positions.detect_ratio_spreads(self.settings.ratio_spreads)
                    current_ids = {self._get_ratio_id(r) for r in current_ratios}

                    # Remove any that are no longer present
                    removed_ids = set(active_ratios.keys()) - current_ids
                    for ratio_id in removed_ids:
                        LOGGER.info("%s ratio spread no longer present, removing from active", ratio_id)
                        del active_ratios[ratio_id]
                        if ratio_id == "call":
                            self.state.call_ratio_state = None
                        elif ratio_id == "put":
                            self.state.put_ratio_state = None
                except Exception:
                    LOGGER.info("failed to verify active ratios, continuing with existing state")
                    pass

                # If all ratios were removed, reset and scan again
                if not active_ratios:
                    await asyncio.sleep(self.settings.poll_interval_seconds)
                    continue

                # Get index price once per cycle
                try:
                    index_price = await self.exchange.get_index_price("BTCUSDT")
                    self.state.last_index_price = index_price
                except Exception as e:
                    LOGGER.warning("Failed to fetch index price: %s", str(e))
                    index_price = None

                # Monitor all active ratios and compute overall portfolio PnL
                total_portfolio_pnl = 0.0
                ratio_pnls: dict[str, float] = {}  # Track individual PnL for logging

                for ratio_id, ratio in active_ratios.items():
                    try:
                        pnl = await self.positions.compute_unrealized_pnl(ratio)
                        ratio_pnls[ratio_id] = pnl
                        total_portfolio_pnl += pnl

                        long_q = await self.exchange.get_best_quote(ratio.long_leg.product_id)
                        short_q = await self.exchange.get_best_quote(ratio.short_leg.product_id)

                        LOGGER.info(
                            "Ratio monitor | type=%s | index=%.2f strike=%.2f ratio=%.2f pnl=%.4f",
                            ratio_id,
                            index_price if index_price else "N/A",
                            ratio.short_leg.strike,
                            ratio.ratio,
                            pnl,
                        )

                        leg_snapshot = {
                            "event": "ratio_monitor_snapshot",
                            "type": ratio_id,
                            "index_price": round(index_price, 4) if index_price else None,
                            "unrealized_pnl": round(pnl, 8),
                            "ratio": round(ratio.ratio, 4),
                            "profit_threshold": self.settings.profit_exit_threshold_usd,
                            "loss_threshold": self.settings.loss_exit_threshold_usd,
                            "long_leg": {
                                "symbol": ratio.long_leg.symbol,
                                "type": ratio.long_leg.option_type,
                                "strike": ratio.long_leg.strike,
                                "quantity": abs(ratio.long_leg.size),
                                "entry_price": ratio.long_leg.entry_price,
                                "best_bid": long_q.best_bid,
                                "best_ask": long_q.best_ask,
                            },
                            "short_leg": {
                                "symbol": ratio.short_leg.symbol,
                                "type": ratio.short_leg.option_type,
                                "strike": ratio.short_leg.strike,
                                "quantity": abs(ratio.short_leg.size),
                                "entry_price": ratio.short_leg.entry_price,
                                "best_bid": short_q.best_bid,
                                "best_ask": short_q.best_ask,
                            },
                        }
                        LOGGER.info("Snapshot: %s", json.dumps(leg_snapshot, separators=(",", ":")))

                    except Exception as e:
                        LOGGER.error("Error monitoring %s ratio: %s", ratio_id, str(e))

                # Log overall portfolio PnL
                pnl_breakdown = ", ".join(f"{k}={v:.2f}" for k, v in ratio_pnls.items())
                LOGGER.info(
                    "Portfolio PnL check | total_pnl=%.4f | breakdown=[%s] | thresholds=[profit=%s, loss=%s]",
                    total_portfolio_pnl,
                    pnl_breakdown,
                    self.settings.profit_exit_threshold_usd,
                    self.settings.loss_exit_threshold_usd,
                )

                # Check exit thresholds on OVERALL portfolio PnL
                if total_portfolio_pnl >= self.settings.profit_exit_threshold_usd or total_portfolio_pnl <= self.settings.loss_exit_threshold_usd:
                    self._set_strategy_state(
                        action="closing all positions",
                        status="overall pnl threshold reached",
                        status_message=(
                            f"closing all ratio spreads | overall pnl={total_portfolio_pnl:.2f}"
                        ),
                        trigger_price=None,
                        trigger_pnl=total_portfolio_pnl,
                    )
                    LOGGER.info(
                        "Overall PnL threshold hit | total_pnl=%.2f | profit=%s loss=%s; closing ALL positions simultaneously",
                        total_portfolio_pnl,
                        self.settings.profit_exit_threshold_usd,
                        self.settings.loss_exit_threshold_usd,
                    )

                    # Close all active ratios
                    for ratio_id, ratio in list(active_ratios.items()):
                        try:
                            await self._close_entire_ratio(ratio)
                            del active_ratios[ratio_id]
                            LOGGER.info("Closed %s ratio", ratio_id)
                        except Exception as e:
                            LOGGER.error("Error closing %s ratio: %s", ratio_id, str(e))

                    # Clear all ratio states
                    self.state.call_ratio_state = None
                    self.state.put_ratio_state = None

                    LOGGER.info("All ratio spreads have been closed")
                    return

                await asyncio.sleep(self.settings.poll_interval_seconds)
        finally:
            if ratio_scan_task is not None and not ratio_scan_task.done():
                ratio_scan_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await ratio_scan_task
