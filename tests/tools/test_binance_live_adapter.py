from __future__ import annotations

from decimal import Decimal

import pytest

from tools.binance_guardrails import BinanceTradeProposal
from tools.binance_live_adapter import (
    BinanceFuturesLiveExecutor,
    BinanceLiveExecutionError,
    SymbolTradingRules,
)


def test_get_symbol_rules_selects_requested_symbol(monkeypatch):
    executor = BinanceFuturesLiveExecutor(base_url="https://example.invalid")

    def _fake_request(method, path, params=None, signed=False):
        assert method == "GET"
        assert path == "/fapi/v1/exchangeInfo"
        return {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "filters": [
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "MIN_NOTIONAL", "notional": "50"},
                    ],
                },
                {
                    "symbol": "DOGEUSDT",
                    "filters": [
                        {"filterType": "MARKET_LOT_SIZE", "stepSize": "1"},
                        {"filterType": "PRICE_FILTER", "tickSize": "0.000010"},
                        {"filterType": "MIN_NOTIONAL", "notional": "5"},
                    ],
                },
            ]
        }

    monkeypatch.setattr(executor, "_request", _fake_request)

    rules = executor._get_symbol_rules("DOGEUSDT")

    assert rules == SymbolTradingRules(
        quantity_step=Decimal("1"),
        price_tick=Decimal("0.000010"),
        min_notional=Decimal("5"),
    )


def test_preview_trade_rejects_below_exchange_min_notional(monkeypatch):
    executor = BinanceFuturesLiveExecutor(base_url="https://example.invalid")
    monkeypatch.setattr(executor, "get_reference_price", lambda symbol: Decimal("0.200000"))
    monkeypatch.setattr(
        executor,
        "_get_symbol_rules",
        lambda symbol: SymbolTradingRules(
            quantity_step=Decimal("1"),
            price_tick=Decimal("0.000010"),
            min_notional=Decimal("5"),
        ),
    )

    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": "DOGEUSDT",
            "side": "BUY",
            "notional_usd": "2",
            "mode": "live",
            "order_type": "MARKET",
            "stop_loss_pct": "0.5",
            "take_profit_pct": "1.0",
            "leverage": "1",
            "verifier_model": "gemini-3.1-flash-lite",
            "verifier_passed": True,
            "verifier_confidence": "0.95",
            "dry_run": False,
        }
    )

    with pytest.raises(BinanceLiveExecutionError, match="below Binance minimum notional 5"):
        executor.preview_trade(proposal)


def test_get_protective_orders_selects_latest_close_position_orders(monkeypatch):
    executor = BinanceFuturesLiveExecutor(base_url="https://example.invalid")

    def _fake_request(method, path, params=None, signed=False):
        assert method == "GET"
        assert path == "/fapi/v1/openOrders"
        assert signed is True
        assert params == {"symbol": "DOGEUSDT"}
        return [
            {"orderId": 1, "symbol": "DOGEUSDT", "side": "SELL", "type": "STOP_MARKET", "closePosition": "true", "stopPrice": "0.09940", "updateTime": 10},
            {"orderId": 2, "symbol": "DOGEUSDT", "side": "SELL", "type": "STOP_MARKET", "closePosition": "true", "stopPrice": "0.09960", "updateTime": 20},
            {"orderId": 3, "symbol": "DOGEUSDT", "side": "SELL", "type": "TAKE_PROFIT_MARKET", "closePosition": "true", "stopPrice": "0.10120", "updateTime": 15},
            {"orderId": 4, "symbol": "DOGEUSDT", "side": "BUY", "type": "STOP_MARKET", "closePosition": "true", "stopPrice": "0.10500", "updateTime": 30},
            {"orderId": 5, "symbol": "DOGEUSDT", "side": "SELL", "type": "LIMIT", "price": "0.10100", "updateTime": 40},
        ]

    monkeypatch.setattr(executor, "_request", _fake_request)

    orders = executor.get_protective_orders("DOGEUSDT", entry_side="BUY")

    assert orders["stop_loss"]["orderId"] == 2
    assert orders["stop_loss_price"] == "0.09960"
    assert orders["take_profit"]["orderId"] == 3
    assert orders["take_profit_price"] == "0.10120"
    assert [order["orderId"] for order in orders["orders"]] == [1, 2, 3]


def test_adjust_protective_orders_places_new_orders_before_cancelling_previous(monkeypatch):
    executor = BinanceFuturesLiveExecutor(base_url="https://example.invalid")
    monkeypatch.setattr(
        executor,
        "_get_symbol_rules",
        lambda symbol: SymbolTradingRules(
            quantity_step=Decimal("1"),
            price_tick=Decimal("0.000010"),
            min_notional=Decimal("5"),
        ),
    )

    calls: list[tuple[str, str, dict[str, str]]] = []

    def _fake_request(method, path, params=None, signed=False):
        recorded_params = dict(params or {})
        calls.append((method, path, recorded_params))
        if method == "POST" and path == "/fapi/v1/order":
            return {
                "orderId": 100 + len([call for call in calls if call[0] == "POST"]),
                "type": recorded_params.get("type"),
                "stopPrice": recorded_params.get("stopPrice"),
            }
        if method == "DELETE" and path == "/fapi/v1/order":
            return {"orderId": recorded_params.get("orderId"), "status": "CANCELED"}
        raise AssertionError(f"Unexpected request: {method} {path} {recorded_params}")

    monkeypatch.setattr(executor, "_request", _fake_request)

    result = executor.adjust_protective_orders(
        "DOGEUSDT",
        entry_side="BUY",
        stop_loss_price=Decimal("0.100219"),
        take_profit_price=Decimal("0.101221"),
        current_orders={
            "orders": [
                {"orderId": 11, "type": "STOP_MARKET"},
                {"orderId": 12, "type": "TAKE_PROFIT_MARKET"},
            ],
            "stop_loss_price": "0.09960",
            "take_profit_price": "0.10120",
        },
    )

    assert [call[0] for call in calls] == ["POST", "POST", "DELETE", "DELETE"]
    assert calls[0][2]["type"] == "STOP_MARKET"
    assert calls[0][2]["stopPrice"] == "0.10021"
    assert calls[1][2]["type"] == "TAKE_PROFIT_MARKET"
    assert calls[1][2]["stopPrice"] == "0.10123"
    assert [call[2]["orderId"] for call in calls[2:]] == [11, 12]
    assert result["new_orders"]["stop_loss_price"] == "0.10021"
    assert result["new_orders"]["take_profit_price"] == "0.10123"