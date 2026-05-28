from __future__ import annotations

from decimal import Decimal

import pytest

from tools.binance_guardrails import (
    BinanceAccountSnapshot,
    BinanceRiskLimits,
    BinanceTradeProposal,
    evaluate_trade_proposal,
)
from tools.binance_live_adapter import (
    BinanceFuturesLiveExecutor,
    BinanceLiveExecutionError,
    SymbolTradingRules,
)
from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage
from tools.doge_grid_advisor import plan_dynamic_grid
from tools.execution_orchestrators import execute_arbitrage, execute_grid, reconcile_grid
from tools.macro_data_oracle import MacroState, MacroTrend, classify_macro_alignment


class _FakeSpotExecutor:
    def __init__(self, *, price: Decimal = Decimal("0.100000"), fail_transfer: bool = False):
        self.price = price
        self.fail_transfer = fail_transfer
        self.orders: list[tuple[str, Decimal]] = []
        self.transfers: list[tuple[str, Decimal, str, str]] = []

    def get_reference_price(self, symbol: str) -> Decimal:
        return self.price

    def place_market_order(self, symbol: str, side: str, quantity: Decimal) -> dict[str, str]:
        self.orders.append((side, quantity))
        return {"symbol": symbol, "side": side, "quantity": format(quantity, "f")}

    def universal_transfer(self, asset: str, amount: Decimal, from_type: str, to_type: str) -> dict[str, str]:
        self.transfers.append((asset, amount, from_type, to_type))
        if self.fail_transfer and from_type == "MAIN":
            raise RuntimeError("transfer failed")
        return {
            "asset": asset,
            "amount": format(amount, "f"),
            "from": from_type,
            "to": to_type,
        }


class _FakeFuturesExecutor:
    def __init__(self, *, fail_order: bool = False):
        self.fail_order = fail_order
        self.margin_calls: list[tuple[str, str]] = []
        self.leverage_calls: list[tuple[str, int]] = []
        self.order_calls: list[dict[str, str]] = []
        self.cancel_calls: list[tuple[str, int]] = []
        self.reference_price = Decimal("0.100000")
        self.active_positions: list[dict[str, str]] = []

    def ensure_margin_type(self, symbol: str, margin_type: str) -> dict[str, str]:
        self.margin_calls.append((symbol, margin_type))
        return {"symbol": symbol, "marginType": margin_type, "status": "UPDATED"}

    def ensure_leverage(self, symbol: str, leverage: int) -> dict[str, int]:
        self.leverage_calls.append((symbol, leverage))
        return {"symbol": symbol, "leverage": leverage}

    def _get_symbol_rules(self, symbol: str) -> SymbolTradingRules:
        return SymbolTradingRules(
            quantity_step=Decimal("1"),
            price_tick=Decimal("0.000010"),
            min_notional=Decimal("5"),
        )

    def _request(self, method: str, path: str, params=None, signed: bool = False):
        params = dict(params or {})
        if method == "POST" and path == "/fapi/v1/order":
            self.order_calls.append(params)
            if self.fail_order:
                raise RuntimeError("futures leg failed")
            return {"orderId": 9001, "status": "FILLED", **params}
        raise AssertionError(f"Unexpected request: {method} {path} {params}")

    def cancel_order(self, symbol: str, order_id: int):
        self.cancel_calls.append((symbol, int(order_id)))
        return {"symbol": symbol, "orderId": int(order_id), "status": "CANCELED"}

    def get_reference_price(self, symbol: str) -> Decimal:
        return self.reference_price

    def fetch_account_overview(self, symbol: str = "") -> dict[str, object]:
        return {
            "symbol": symbol,
            "active_positions": list(self.active_positions),
            "account_snapshot": {},
            "asset": "USDT",
        }


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("BINANCE_KILL_SWITCH", raising=False)
    monkeypatch.setenv("BINANCE_RISK_MAX_NOTIONAL_USD", "250")


def test_plan_delta_neutral_arbitrage_defaults_to_conservative_leverage():
    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    assert plan.leverage == Decimal("2")
    assert plan.futures_margin_usd == pytest.approx(Decimal("3.333333333333333333333333333"))
    assert plan.delta_gap_pct == Decimal("0")


def test_plan_delta_neutral_arbitrage_rejects_excessive_leverage():
    with pytest.raises(ValueError, match="exceeds configured cap"):
        plan_delta_neutral_arbitrage(
            symbol="DOGEUSDT",
            available_capital_usd=Decimal("10"),
            market_price=Decimal("0.10"),
            funding_rate=Decimal("0.0020"),
            leverage=Decimal("3"),
        )


def test_execute_arbitrage_dry_run_reports_monetary_transfer(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spot = _FakeSpotExecutor()
    futures = _FakeFuturesExecutor()
    monkeypatch.setattr("tools.execution_orchestrators.BinanceSpotLiveExecutor.from_env", lambda: spot)
    monkeypatch.setattr("tools.execution_orchestrators.BinanceFuturesLiveExecutor.from_env", lambda: futures)

    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    result = execute_arbitrage(plan, dry_run=True)

    assert result["success"] is True
    assert result["transfer_amount"] == pytest.approx(float(plan.futures_margin_usd))
    assert result["execution_state"]["status"] == "dry_run_preview"


def test_execute_arbitrage_compensates_when_futures_leg_fails(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spot = _FakeSpotExecutor()
    futures = _FakeFuturesExecutor(fail_order=True)
    monkeypatch.setattr("tools.execution_orchestrators.BinanceSpotLiveExecutor.from_env", lambda: spot)
    monkeypatch.setattr("tools.execution_orchestrators.BinanceFuturesLiveExecutor.from_env", lambda: futures)

    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    result = execute_arbitrage(plan, dry_run=False)

    assert result["success"] is False
    assert result["compensation"]["success"] is True
    assert [action["step"] for action in result["compensation"]["actions"]] == [
        "transfer_back",
        "spot_unwind",
    ]
    assert result["execution_state"]["status"] == "compensated"


def test_execute_arbitrage_replays_completed_execution_without_duplicate_orders(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    spot = _FakeSpotExecutor()
    futures = _FakeFuturesExecutor()
    monkeypatch.setattr("tools.execution_orchestrators.BinanceSpotLiveExecutor.from_env", lambda: spot)
    monkeypatch.setattr("tools.execution_orchestrators.BinanceFuturesLiveExecutor.from_env", lambda: futures)

    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    first = execute_arbitrage(plan, dry_run=False)
    second = execute_arbitrage(plan, dry_run=False)

    assert first["success"] is True
    assert second["success"] is True
    assert second["replayed"] is True
    assert len(spot.orders) == 1
    assert len(futures.order_calls) == 1


def test_ensure_margin_type_tolerates_unchanged_setting(monkeypatch):
    executor = BinanceFuturesLiveExecutor(base_url="https://example.invalid")

    def _fake_request(method, path, params=None, signed=False):
        raise BinanceLiveExecutionError("Binance API POST /fapi/v1/marginType failed: No need to change margin type.")

    monkeypatch.setattr(executor, "_request", _fake_request)

    result = executor.ensure_margin_type("DOGEUSDT", "ISOLATED")

    assert result["status"] == "UNCHANGED"


def test_plan_dynamic_grid_marks_trending_regime_as_not_enterable():
    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.002"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.03"),
    )

    assert plan.regime_allows_entry is False
    assert plan.regime == "trend"


def test_execute_grid_sets_margin_and_leverage_before_orders(monkeypatch):
    futures = _FakeFuturesExecutor()
    monkeypatch.setattr("tools.execution_orchestrators.BinanceFuturesLiveExecutor.from_env", lambda: futures)

    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("30"),
        grids_per_side=2,
        trend_bias_pct=Decimal("0.002"),
        leverage=Decimal("1"),
    )

    result = execute_grid(plan, dry_run=False)

    assert result["success"] is True
    assert futures.margin_calls == [("DOGEUSDT", "ISOLATED")]
    assert futures.leverage_calls == [("DOGEUSDT", 1)]
    assert len(futures.order_calls) == len(plan.levels)


def test_reconcile_grid_cancels_orders_and_blocks_reentry(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    futures = _FakeFuturesExecutor()
    monkeypatch.setattr("tools.execution_orchestrators.BinanceFuturesLiveExecutor.from_env", lambda: futures)

    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("30"),
        grids_per_side=2,
        trend_bias_pct=Decimal("0.002"),
        leverage=Decimal("1"),
    )
    seeded = execute_grid(plan, dry_run=False)
    assert seeded["success"] is True

    futures.reference_price = Decimal("0.110000")
    futures.active_positions = [
        {
            "symbol": "DOGEUSDT",
            "position_amt": "12",
            "entry_price": "0.100000",
            "side": "LONG",
        }
    ]

    reconciled = reconcile_grid(symbol="DOGEUSDT")

    assert reconciled["success"] is True
    assert reconciled["checked"] == 1
    assert len(reconciled["reconciled"]) == 1
    item = reconciled["reconciled"][0]
    assert item["status"] == "stopped_breakout"
    assert item["breakout_side"] == "above_upper_bound"
    assert item["residual_position"]["position_amt"] == "12"
    assert len(futures.cancel_calls) == len(plan.levels)


def test_classify_macro_alignment_blocks_bearish_high_volatility():
    macro_state = MacroState(
        btc_trend_1h=MacroTrend.BEARISH,
        btc_trend_4h=MacroTrend.BEARISH,
        global_funding_bias="negative",
        risk_level="high_volatility",
        rationale="macro stress",
    )

    assert classify_macro_alignment(macro_state) == "blocked"


def test_evaluate_trade_proposal_blocks_macro_alignment():
    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": "DOGEUSDT",
            "side": "BUY",
            "notional_usd": "5.25",
            "mode": "live",
            "order_type": "MARKET",
            "stop_loss_pct": "0.5",
            "take_profit_pct": "1.0",
            "leverage": "1",
            "verifier_model": "gemini-3.1-flash-lite",
            "verifier_passed": True,
            "verifier_confidence": "0.95",
            "dry_run": False,
            "macro_alignment": "blocked",
        }
    )
    account = BinanceAccountSnapshot.from_payload(
        {
            "free_balance_usd": "100",
            "open_positions": 0,
            "positions_in_symbol": 0,
            "daily_realized_pnl_usd": "0",
            "kill_switch_active": False,
        }
    )
    limits = BinanceRiskLimits.from_env(
        {
            "BINANCE_RISK_MODE": "live",
            "BINANCE_LIVE_TRADING_ENABLED": "true",
            "BINANCE_RISK_ALLOWED_SYMBOLS": "DOGEUSDT",
            "BINANCE_RISK_MAX_NOTIONAL_USD": "10",
            "BINANCE_RISK_MIN_FREE_BALANCE_USD": "10",
            "BINANCE_RISK_MAX_DAILY_LOSS_USD": "25",
            "BINANCE_RISK_MAX_OPEN_POSITIONS": "1",
            "BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL": "1",
            "BINANCE_RISK_MAX_LEVERAGE": "1",
            "BINANCE_RISK_MIN_VERIFIER_CONFIDENCE": "0.75",
        }
    )

    decision = evaluate_trade_proposal(proposal, account, limits, kill_switch_active=False)

    assert decision.allowed is False
    assert "macro alignment blocks new entries" in decision.reasons


def test_evaluate_trade_proposal_halves_effective_notional_on_divergent_macro():
    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": "DOGEUSDT",
            "side": "BUY",
            "notional_usd": "6.0",
            "mode": "live",
            "order_type": "MARKET",
            "stop_loss_pct": "0.5",
            "take_profit_pct": "1.0",
            "leverage": "1",
            "verifier_model": "gemini-3.1-flash-lite",
            "verifier_passed": True,
            "verifier_confidence": "0.95",
            "dry_run": False,
            "macro_alignment": "divergent",
        }
    )
    account = BinanceAccountSnapshot.from_payload(
        {
            "free_balance_usd": "100",
            "open_positions": 0,
            "positions_in_symbol": 0,
            "daily_realized_pnl_usd": "0",
            "kill_switch_active": False,
        }
    )
    limits = BinanceRiskLimits.from_env(
        {
            "BINANCE_RISK_MODE": "live",
            "BINANCE_LIVE_TRADING_ENABLED": "true",
            "BINANCE_RISK_ALLOWED_SYMBOLS": "DOGEUSDT",
            "BINANCE_RISK_MAX_NOTIONAL_USD": "10",
            "BINANCE_RISK_MIN_FREE_BALANCE_USD": "10",
            "BINANCE_RISK_MAX_DAILY_LOSS_USD": "25",
            "BINANCE_RISK_MAX_OPEN_POSITIONS": "1",
            "BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL": "1",
            "BINANCE_RISK_MAX_LEVERAGE": "1",
            "BINANCE_RISK_MIN_VERIFIER_CONFIDENCE": "0.75",
        }
    )

    decision = evaluate_trade_proposal(proposal, account, limits, kill_switch_active=False)

    assert decision.allowed is False
    assert any(
        "effective_max_notional 5.0" in reason and "Macro Alignment: divergent" in reason
        for reason in decision.reasons
    )
