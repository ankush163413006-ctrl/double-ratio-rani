from __future__ import annotations

import asyncio
import importlib
import sys
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src.models import OptionLeg, Quote


class FakeExchange:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def get_index_price(self, symbol: str) -> float:
        return 100.0

    async def parse_option_positions(self):
        return []

    async def get_best_quote(self, product_id: int):
        return Quote(best_bid=100.0, best_ask=101.0)


class FakeExecutor:
    async def execute_market_single_submission_with_fill_confirmation(self, **kwargs):
        return 1.0


@pytest.fixture
def api_module(monkeypatch):
    monkeypatch.setenv("DELTA_API_KEY", "key")
    monkeypatch.setenv("DELTA_API_SECRET", "secret")
    monkeypatch.setenv("DELTA_BASE_URL", "https://example.com")
    monkeypatch.setenv("DELTA_SSL_VERIFY", "false")
    monkeypatch.setenv("POLL_INTERVAL_SECONDS", "0")
    monkeypatch.setenv("PNL_PROFIT_THRESHOLD_USD", "10")
    monkeypatch.setenv("PNL_LOSS_THRESHOLD_USD", "-20")
    monkeypatch.setenv("RATIO_SPREADS", "1:2")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    import src.exchange_client as exchange_client_module
    exchange_client_module.DeltaExchangeClient = FakeExchange

    sys.modules.pop("src.api", None)
    api_module = importlib.import_module("src.api")
    return api_module


def test_health_endpoint(api_module) -> None:
    client = TestClient(api_module.app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_start_and_status_endpoints(api_module) -> None:
    api_module.manager.strategy.run = AsyncMock()

    client = TestClient(api_module.app)
    start_resp = client.post("/start")
    assert start_resp.status_code == 200
    assert start_resp.json()["status"] == "started"

    api_module.manager._task = MagicMock()
    api_module.manager._task.done.return_value = False

    status_resp = client.get("/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["running"] is True
    assert status_resp.json()["strategy_state"]["profit_exit_threshold_usd"] == 10.0
    assert status_resp.json()["strategy_state"]["loss_exit_threshold_usd"] == -20.0


def test_summary_endpoint_returns_strategy_snapshot(api_module) -> None:
    api_module.manager.strategy.exchange = FakeExchange()
    api_module.manager.strategy.state.status_message = "running"
    api_module.manager.strategy.state.action = "monitoring ratio-spreads"
    api_module.manager.strategy.state.status = "monitoring pnl thresholds"

    client = TestClient(api_module.app)
    resp = client.get("/summary")
    assert resp.status_code == 200
    assert "unrealized_pnl" in resp.json()
    assert "positions" in resp.json()
    assert resp.json()["profit_exit_threshold_usd"] == 10.0
    assert resp.json()["loss_exit_threshold_usd"] == -20.0
