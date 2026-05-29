from __future__ import annotations

import os
from decimal import Decimal
from types import SimpleNamespace

import pytest

import agent.transports.binance_guarded_mcp_server as guarded
from tools.binance_live_adapter import BinanceLiveExecutionError
from tools.binance_paper_runtime import record_market_evidence, request_trade_approval, record_trade_approval


class _FakeExecutor:
    def __init__(self):
        self.submit_calls = []
        self.preview_calls = []

    def fetch_account_overview(self, symbol=None):
        return {
            "symbol": symbol,
            "account_snapshot": {
                "free_balance_usd": "1000",
                "open_positions": 0,
                "positions_in_symbol": 0,
                "daily_realized_pnl_usd": "0",
                "kill_switch_active": False,
            },
            "active_positions": [],
            "asset": "USDT",
        }

    def submit_trade(self, proposal):
        self.submit_calls.append(proposal)
        return {"entry_order": {"symbol": proposal.symbol, "status": "FILLED"}}

    def get_reference_price(self, symbol):
        assert symbol == "BTCUSDT"
        return Decimal("100")

    def preview_trade(self, proposal):
        self.preview_calls.append(proposal)
        return {
            "symbol": proposal.symbol,
            "side": proposal.side,
            "quantity": "1",
            "reference_price": "100",
            "estimated_notional_usd": "100",
            "rules": {
                "quantity_step": "0.001",
                "price_tick": "0.1",
                "min_notional": "5",
            },
        }


class _SerializableSignal(SimpleNamespace):
    def to_dict(self):
        payload = {}
        for key, value in self.__dict__.items():
            payload[key] = format(value, "f") if isinstance(value, Decimal) else value
        return payload


@pytest.fixture(autouse=True)
def _isolate_runtime_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("BINANCE_KILL_SWITCH", raising=False)
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "false")
    monkeypatch.setenv("BINANCE_RISK_ALLOWED_SYMBOLS", "BTCUSDT,DOGEUSDT")
    monkeypatch.setenv("BINANCE_RISK_MAX_NOTIONAL_USD", "250")
    monkeypatch.setenv("BINANCE_RISK_MIN_FREE_BALANCE_USD", "10")
    monkeypatch.setenv("BINANCE_RISK_MAX_DAILY_LOSS_USD", "25")
    monkeypatch.setenv("BINANCE_RISK_MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL", "1")
    monkeypatch.setenv("BINANCE_RISK_MAX_LEVERAGE", "1")
    monkeypatch.setenv("BINANCE_RISK_MIN_VERIFIER_CONFIDENCE", "0.75")


def test_account_snapshot_result_uses_live_executor(monkeypatch):
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: _FakeExecutor())

    result = guarded._account_snapshot_result("BTCUSDT")

    assert result["success"] is True
    assert result["symbol"] == "BTCUSDT"
    assert result["account_snapshot"]["free_balance_usd"] == "1000"


def test_submit_trade_result_live_revalidates_with_exchange_snapshot(monkeypatch):
    executor = _FakeExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="SELL",
        notional_usd=100,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=99,
        positions_in_symbol=99,
        daily_realized_pnl_usd=-999.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="short breakout failure",
        dry_run=False,
    )

    assert result["success"] is True
    assert result["execution_mode"] == "live"
    assert executor.submit_calls
    assert result["decision"]["account"]["free_balance_usd"] == "1000"


def test_submit_trade_result_live_blocks_blocked_macro_alignment(monkeypatch):
    executor = _FakeExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_ALLOWED_SYMBOLS", "DOGEUSDT")
    monkeypatch.setenv("BINANCE_RISK_MAX_NOTIONAL_USD", "10")
    monkeypatch.setenv("BINANCE_RISK_MIN_FREE_BALANCE_USD", "10")
    monkeypatch.setenv("BINANCE_RISK_MAX_DAILY_LOSS_USD", "25")
    monkeypatch.setenv("BINANCE_RISK_MAX_OPEN_POSITIONS", "1")
    monkeypatch.setenv("BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL", "1")
    monkeypatch.setenv("BINANCE_RISK_MAX_LEVERAGE", "1")
    monkeypatch.setenv("BINANCE_RISK_MIN_VERIFIER_CONFIDENCE", "0.75")

    result = guarded._submit_trade_result(
        symbol="DOGEUSDT",
        side="BUY",
        notional_usd=5,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="respect macro blocks",
        macro_alignment="blocked",
        dry_run=True,
    )

    assert result["success"] is False
    assert result["decision"]["proposal"]["macro_alignment"] == "blocked"
    assert "macro alignment blocks new entries" in result["decision"]["reasons"]
    assert not executor.preview_calls


def test_submit_trade_result_live_dry_run_uses_exchange_snapshot(monkeypatch):
    executor = _FakeExecutor()

    def _zero_balance_overview(symbol=None):
        return {
            "symbol": symbol,
            "account_snapshot": {
                "free_balance_usd": "0",
                "open_positions": 0,
                "positions_in_symbol": 0,
                "daily_realized_pnl_usd": "0",
                "kill_switch_active": False,
            },
            "active_positions": [],
            "asset": "USDT",
        }

    executor.fetch_account_overview = _zero_balance_overview
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="BUY",
        notional_usd=20,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=1000.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="live dry run readiness",
        dry_run=True,
    )

    assert result["success"] is False
    assert "live trade rejected by risk guardrails after refreshing account snapshot" in result["error"]
    assert result["decision"]["account"]["free_balance_usd"] == "0"
    assert not executor.submit_calls


def test_submit_trade_result_live_dry_run_surfaces_exchange_rule_error(monkeypatch):
    executor = _FakeExecutor()

    def _raise_preview_error(proposal):
        raise BinanceLiveExecutionError("below Binance minimum notional 5 for DOGEUSDT")

    executor.preview_trade = _raise_preview_error
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")

    result = guarded._submit_trade_result(
        symbol="DOGEUSDT",
        side="BUY",
        notional_usd=2,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        leverage=1.0,
        free_balance_usd=1000.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="live dry run readiness",
        dry_run=True,
    )

    assert result["success"] is False
    assert "below Binance minimum notional 5 for DOGEUSDT" in result["error"]
    assert not executor.submit_calls


def test_submit_trade_result_live_requires_approval_when_enabled(monkeypatch, tmp_path):
    executor = _FakeExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "true")

    result = guarded._submit_trade_result(
        symbol="DOGEUSDT",
        side="BUY",
        notional_usd=20,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="live breakout",
        dry_run=False,
    )

    assert result["success"] is False
    assert "approval_id '' was not found" in result["error"]
    assert not executor.submit_calls


def test_submit_trade_result_live_executes_after_approved_live_request(monkeypatch, tmp_path):
    executor = _FakeExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "true")

    proposal = guarded.BinanceTradeProposal.from_payload(
        {
            "symbol": "DOGEUSDT",
            "side": "BUY",
            "notional_usd": "20",
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
    approval = request_trade_approval(proposal, home=tmp_path)
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)

    result = guarded._submit_trade_result(
        symbol="DOGEUSDT",
        side="BUY",
        notional_usd=20,
        mode="live",
        order_type="MARKET",
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="live breakout",
        dry_run=False,
        approval_id=approval["approval_id"],
    )

    assert result["success"] is True
    assert result["execution_mode"] == "live"
    assert result["approval"]["status"] == "consumed"
    assert result["live_execution_event"]["event_type"] == "live_trade_executed"
    assert executor.submit_calls


def test_submit_trade_result_paper_dry_run_allowed_under_live_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_ALLOWED_SYMBOLS", "DOGEUSDT")
    monkeypatch.setenv("BINANCE_RISK_MAX_NOTIONAL_USD", "5.25")
    monkeypatch.setenv("BINANCE_RISK_MIN_FREE_BALANCE_USD", "2")
    monkeypatch.setenv("BINANCE_PAPER_STARTING_BALANCE_USD", "1000")

    result = guarded._submit_trade_result(
        symbol="DOGEUSDT",
        side="BUY",
        notional_usd=5,
        mode="paper",
        order_type="MARKET",
        stop_loss_pct=0.5,
        take_profit_pct=1.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="paper rehearsal under live runtime",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["execution_mode"] == "dry_run"
    assert result["decision"]["proposal"]["mode"] == "paper"
    assert result["decision"]["limits"]["mode"] == "paper"


def test_submit_trade_result_auto_mode_follows_live_profile(monkeypatch, tmp_path):
    executor = _FakeExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="BUY",
        notional_usd=20,
        mode="auto",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="follow active mode",
        dry_run=True,
    )

    assert result["success"] is True
    assert result["execution_mode"] == "dry_run"
    assert result["decision"]["proposal"]["mode"] == "live"
    assert executor.preview_calls


def test_submit_trade_result_paper_requires_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "paper")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "true")
    monkeypatch.setattr(guarded, "_send_whatsapp_home_message", lambda message: {"success": True, "message": message})

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="BUY",
        notional_usd=50,
        mode="paper",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="paper breakout",
        dry_run=False,
        approval_id="",
        evidence_id="",
        use_persistent_account=True,
        notify_whatsapp=False,
    )

    assert result["success"] is False
    assert "approval_id" in result["error"]


def test_submit_trade_result_paper_opens_position_after_approval(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "paper")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "true")
    monkeypatch.setenv("BINANCE_PAPER_STARTING_BALANCE_USD", "1000")
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=False: _FakeExecutor())
    monkeypatch.setattr(guarded, "_send_whatsapp_home_message", lambda message: {"success": True, "message": message})

    evidence = record_market_evidence(
        symbol="BTCUSDT",
        timeframe="15m",
        binance_reference_price=Decimal("100"),
        external_reference_price=Decimal("101"),
        market_summary="Binance and external tape agree on momentum continuation.",
        source_urls=["https://www.binance.com/en/futures/BTCUSDT", "https://www.coingecko.com/en/coins/bitcoin"],
        external_source_name="CoinGecko",
        momentum_summary="Volume confirmation is positive.",
        home=tmp_path,
    )
    approval = request_trade_approval(
        guarded.BinanceTradeProposal.from_payload(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "notional_usd": "50",
                "mode": "paper",
                "order_type": "MARKET",
                "stop_loss_pct": "1.0",
                "take_profit_pct": "2.0",
                "leverage": "1",
                "verifier_model": "gemini-3.1-flash-lite",
                "verifier_passed": True,
                "verifier_confidence": "0.95",
                "dry_run": False,
            }
        ),
        evidence_id=evidence["evidence_id"],
        decision_context={"selected_strategy": {"strategy_id": "overlay_tactical_long"}},
        home=tmp_path,
    )
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="BUY",
        notional_usd=50,
        mode="paper",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="paper breakout",
        dry_run=False,
        approval_id=approval["approval_id"],
        evidence_id=evidence["evidence_id"],
        use_persistent_account=True,
        notify_whatsapp=False,
    )

    assert result["success"] is True
    assert result["execution_mode"] == "paper"
    assert result["paper_position"]["symbol"] == "BTCUSDT"
    assert result["paper_position"]["decision_context"]["selected_strategy"]["strategy_id"] == "overlay_tactical_long"
    assert result["paper_account"]["free_balance_usd"] == "950"


def test_submit_trade_result_paper_formats_professional_whatsapp_message(monkeypatch, tmp_path):
    sent_messages: list[str] = []

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_RISK_MODE", "paper")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "false")
    monkeypatch.setenv("BINANCE_REQUIRE_TRADE_APPROVAL", "true")
    monkeypatch.setenv("BINANCE_PAPER_STARTING_BALANCE_USD", "1000")
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=False: _FakeExecutor())
    monkeypatch.setattr(
        guarded,
        "_send_whatsapp_home_message",
        lambda message: sent_messages.append(message) or {"success": True, "message": message},
    )

    evidence = record_market_evidence(
        symbol="BTCUSDT",
        timeframe="15m",
        binance_reference_price=Decimal("100"),
        external_reference_price=Decimal("101"),
        market_summary="Binance and external tape agree on momentum continuation.",
        source_urls=["https://www.binance.com/en/futures/BTCUSDT", "https://www.coingecko.com/en/coins/bitcoin"],
        external_source_name="CoinGecko",
        momentum_summary="Volume confirmation is positive.",
        home=tmp_path,
    )
    approval = request_trade_approval(
        guarded.BinanceTradeProposal.from_payload(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "notional_usd": "50",
                "mode": "paper",
                "order_type": "MARKET",
                "stop_loss_pct": "1.0",
                "take_profit_pct": "2.0",
                "leverage": "1",
                "verifier_model": "gemini-3.1-flash-lite",
                "verifier_passed": True,
                "verifier_confidence": "0.95",
                "dry_run": False,
            }
        ),
        evidence_id=evidence["evidence_id"],
        home=tmp_path,
    )
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)

    result = guarded._submit_trade_result(
        symbol="BTCUSDT",
        side="BUY",
        notional_usd=50,
        mode="paper",
        order_type="MARKET",
        stop_loss_pct=1.0,
        take_profit_pct=2.0,
        leverage=1.0,
        free_balance_usd=0.0,
        open_positions=0,
        positions_in_symbol=0,
        daily_realized_pnl_usd=0.0,
        verifier_model="gemini-3.1-flash-lite",
        verifier_passed=True,
        verifier_confidence=0.95,
        rationale="paper breakout",
        dry_run=False,
        approval_id=approval["approval_id"],
        evidence_id=evidence["evidence_id"],
        use_persistent_account=True,
        notify_whatsapp=True,
    )

    assert result["success"] is True
    assert sent_messages
    assert "Fill:" in sent_messages[-1]
    assert "Riesgo max" in sent_messages[-1]
    assert "R/B 2" in sent_messages[-1]
    assert "ESTADO TRADE-" in sent_messages[-1]
    assert "CERRAR PPOS-" in sent_messages[-1]


def test_paper_position_status_result_resolves_trade_id_with_market_snapshot(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BINANCE_PAPER_STARTING_BALANCE_USD", "1000")
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=False: _FakeExecutor())

    evidence = record_market_evidence(
        symbol="BTCUSDT",
        timeframe="15m",
        binance_reference_price=Decimal("100"),
        external_reference_price=Decimal("101"),
        market_summary="Two-source confirmation",
        source_urls=["https://www.binance.com/en/futures/BTCUSDT", "https://www.coingecko.com/en/coins/bitcoin"],
        home=tmp_path,
    )
    approval = request_trade_approval(
        guarded.BinanceTradeProposal.from_payload(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "notional_usd": "50",
                "mode": "paper",
                "order_type": "MARKET",
                "stop_loss_pct": "1.0",
                "take_profit_pct": "2.0",
                "leverage": "1",
                "verifier_model": "gemini-3.1-flash-lite",
                "verifier_passed": True,
                "verifier_confidence": "0.95",
                "dry_run": False,
            }
        ),
        evidence_id=evidence["evidence_id"],
        home=tmp_path,
    )
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)
    guarded.open_paper_position(
        guarded.BinanceTradeProposal.from_payload(
            {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "notional_usd": "50",
                "mode": "paper",
                "order_type": "MARKET",
                "stop_loss_pct": "1.0",
                "take_profit_pct": "2.0",
                "leverage": "1",
                "verifier_model": "gemini-3.1-flash-lite",
                "verifier_passed": True,
                "verifier_confidence": "0.95",
                "dry_run": False,
            }
        ),
        reference_price=Decimal("100"),
        approval_id=approval["approval_id"],
        evidence_id=evidence["evidence_id"],
        home=tmp_path,
    )

    status = guarded._paper_position_status_result(reference_id=approval["approval_id"])

    assert status["success"] is True
    assert status["status"] == "open"
    assert status["market_price"] == "100"
    assert status["commands"]["status_trade"] == f"ESTADO {approval['approval_id']}"


def test_build_paper_close_whatsapp_message_includes_pnl_reason_and_duration():
    message = guarded._build_paper_close_whatsapp_message(
        {
            "position": {
                "symbol": "DOGEUSDT",
                "side": "BUY",
                "position_id": "PPOS-123",
                "approval_id": "TRADE-123",
            },
            "closed_at": "2026-05-18T21:19:00+00:00",
            "exit_price": "0.105343",
            "trigger": "take_profit",
            "realized_pnl_usd": "0.2",
            "realized_pnl_pct": "1",
            "duration_human": "4m 10s",
            "reason": "protective exit triggered via take_profit",
            "commands": {"status_trade": "ESTADO TRADE-123"},
        }
    )

    assert "PnL 0.20 USD (1.00%)" in message
    assert "Motivo: protective exit triggered via take_profit" in message
    assert "Duracion 4m 10s" in message
    assert "Seguimiento: ESTADO TRADE-123" in message


def test_build_paper_status_whatsapp_message_for_open_position():
    message = guarded._build_paper_status_whatsapp_message(
        {
            "success": True,
            "status": "open",
            "position": {
                "symbol": "BTCUSDT",
                "side": "BUY",
                "position_id": "PPOS-123",
                "approval_id": "TRADE-123",
                "entry_price": "100",
                "notional_usd": "50",
                "stop_loss_price": "99",
                "take_profit_price": "102",
                "opened_at": "2026-05-19T00:00:00+00:00",
            },
            "opened_at": "2026-05-19T00:00:00+00:00",
            "risk": {
                "notional_usd": "50",
                "estimated_max_loss_usd": "0.5",
                "risk_reward_ratio": "2",
                "stop_loss_price": "99",
                "take_profit_price": "102",
            },
            "market_price": "101",
            "unrealized_pnl_usd": "0.5",
            "unrealized_pnl_pct": "1",
            "duration_human": "5m 0s",
            "commands": {
                "status_trade": "ESTADO TRADE-123",
                "close_position": "CERRAR PPOS-123",
            },
        }
    )

    assert "Paper activo BTCUSDT BUY | PPOS-123 | TRADE-123" in message
    assert "Mercado 101.00" in message
    assert "PnL flotante 0.50 USD (1.00%)" in message
    assert "Seguimiento: ESTADO TRADE-123 | CERRAR PPOS-123" in message


def test_build_paper_daily_summary_whatsapp_message_reports_daily_counts():
    message = guarded._build_paper_daily_summary_whatsapp_message(
        {
            "date": "2026-05-19",
            "entries_count": 2,
            "exits_count": 1,
            "realized_pnl_usd": "1.25",
            "approvals_requested": 3,
            "approvals_approved": 2,
            "approvals_denied": 1,
            "open_positions_count": 1,
            "open_positions": [{"symbol": "DOGEUSDT", "side": "BUY"}],
            "doge_strategy_scorecard": {
                "total_matches": 2,
                "approval_conversion_pct": "50",
                "expectancy_usd": "0.15",
                "median_hold_human": "5m 0s",
                "strategy_regime_pairs": [
                    {"strategy_id": "overlay_tactical_long", "regime_label": "breakout_trend"}
                ],
            },
        }
    )

    assert "Resumen paper 2026-05-19" in message
    assert "Entradas 2 | Salidas 1 | PnL realizado 1.25 USD" in message
    assert "Aprobaciones pedidas 3 | Aprobadas 2 | Rechazadas 1" in message
    assert "Abiertas: DOGEUSDT BUY" in message
    assert "Scorecard: DOGE conv 50% | expectancy 0.15 USD | hold med 5m 0s | top overlay_tactical_long x breakout_trend" in message


def test_runtime_env_reloads_when_env_file_changes(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text("BINANCE_RISK_ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT\n", encoding="utf-8")

    monkeypatch.setattr(guarded, "get_env_path", lambda: env_path)
    monkeypatch.setattr(guarded, "_LOADED_ENV_PATH", None)
    monkeypatch.setattr(guarded, "_LOADED_ENV_MTIME_NS", None)
    monkeypatch.delenv("BINANCE_RISK_ALLOWED_SYMBOLS", raising=False)

    guarded._ensure_runtime_env_loaded()
    assert os.environ.get("BINANCE_RISK_ALLOWED_SYMBOLS") == "BTCUSDT,ETHUSDT"

    previous_mtime_ns = env_path.stat().st_mtime_ns
    env_path.write_text("BINANCE_RISK_ALLOWED_SYMBOLS=BTCUSDT,ETHUSDT,DOGEUSDT\n", encoding="utf-8")
    if env_path.stat().st_mtime_ns == previous_mtime_ns:
        os.utime(env_path, ns=(previous_mtime_ns + 1, previous_mtime_ns + 1))

    guarded._ensure_runtime_env_loaded()
    assert os.environ.get("BINANCE_RISK_ALLOWED_SYMBOLS") == "BTCUSDT,ETHUSDT,DOGEUSDT"


def test_runtime_env_overrides_stale_inherited_values_on_first_load(monkeypatch, tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        "BINANCE_RISK_MODE=live\nBINANCE_LIVE_TRADING_ENABLED=true\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(guarded, "get_env_path", lambda: env_path)
    monkeypatch.setattr(guarded, "_LOADED_ENV_PATH", None)
    monkeypatch.setattr(guarded, "_LOADED_ENV_MTIME_NS", None)
    monkeypatch.setenv("BINANCE_RISK_MODE", "paper")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "false")

    guarded._ensure_runtime_env_loaded()

    assert os.environ.get("BINANCE_RISK_MODE") == "live"
    assert os.environ.get("BINANCE_LIVE_TRADING_ENABLED") == "true"


def test_adjust_live_trade_protection_result_rearms_live_orders(monkeypatch):
    class _AdjustmentExecutor:
        def __init__(self):
            self.adjust_calls = []

        def adjust_protective_orders(self, symbol, **kwargs):
            self.adjust_calls.append((symbol, kwargs))
            return {"symbol": symbol, "new_orders": {"stop_loss_price": "0.10020", "take_profit_price": "0.10120"}}

    executor = _AdjustmentExecutor()
    monkeypatch.setattr(guarded, "_get_live_executor", lambda require_credentials=True: executor)
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_ALLOWED_SYMBOLS", "DOGEUSDT")
    monkeypatch.setattr(
        guarded,
        "build_doge_live_management_snapshot",
        lambda *args, **kwargs: SimpleNamespace(
            symbol="DOGEUSDT",
            timeframe="15m",
            approval_id="TRADE-123",
            approval={"approval_id": "TRADE-123", "decision_context": {"selected_strategy": {"strategy_id": "overlay_tactical_long"}}},
            entry_side="BUY",
            signal=_SerializableSignal(last_close=Decimal("0.10080"), verdict="manage"),
            contextual_signals={},
            active_position={"entry_price": "0.10000", "side": "LONG"},
            plan=SimpleNamespace(
                action="trail_profit",
                summary="subir SL para asegurar buena parte del beneficio",
                rationale="La jugada ya recorrio casi todo el objetivo.",
                unrealized_pnl_usd=Decimal("0.40"),
                pnl_pct=Decimal("0.80"),
                higher_timeframe_support=2,
                higher_timeframe_total=2,
            ),
            protective_orders_missing=False,
                protective_orders={
                    "orders": [{"orderId": 11}, {"orderId": 12}],
                    "stop_loss": {"orderId": 11},
                    "take_profit": {"orderId": 12},
                    "stop_loss_price": "0.09950",
                    "take_profit_price": "0.10100",
                },
            current_stop_price=Decimal("0.09950"),
            current_take_profit_price=Decimal("0.10100"),
            recommended_stop_price=Decimal("0.10020"),
            recommended_take_profit_price=Decimal("0.10120"),
            actionable_adjustment=True,
        ),
    )

    result = guarded._adjust_live_trade_protection_result(symbol="DOGEUSDT")

    assert result["success"] is True
    assert executor.adjust_calls
    assert executor.adjust_calls[0][0] == "DOGEUSDT"
    assert result["adjustment_event"]["event_type"] == "live_trade_protection_adjusted"
    assert result["adjustment_event"]["decision_context"]["selected_strategy"]["strategy_id"] == "overlay_tactical_long"
    message = guarded._build_live_adjustment_whatsapp_message(result)
    assert "Ajuste live DOGEUSDT | TRADE-123" in message
    assert "SL 0.099500 -> 0.100200" in message
    assert "esperar radar 15m" in message


def test_resolve_doge_premium_analysis_request_denied_entry_falls_back_to_trade_approval(monkeypatch):
    request = {
        "request_id": "PREM-123",
        "status": "pending",
        "symbol": "DOGEUSDT",
        "request_kind": "entry",
        "model": "gemini-3.5-flash",
        "material_payload": {
            "proposal_payload": {
                "symbol": "DOGEUSDT",
                "side": "BUY",
                "notional_usd": "5.25",
                "mode": "live",
                "order_type": "MARKET",
                "stop_loss_pct": "0.5",
                "take_profit_pct": "1.0",
                "leverage": "1",
                "verifier_model": "doge-scout-v1",
                "verifier_passed": True,
                "verifier_confidence": "0.88",
                "dry_run": False,
            },
            "market_summary": "DOGE mantiene sesgo alcista.",
            "evidence_id": "EVID-123",
        },
    }

    monkeypatch.setattr(guarded, "get_latest_doge_premium_analysis_request", lambda **kwargs: request)
    monkeypatch.setattr(
        guarded,
        "record_doge_premium_analysis_decision",
        lambda request_id, **kwargs: {**request, "request_id": request_id, "status": "denied"},
    )
    monkeypatch.setattr(
        guarded,
        "_ensure_trade_approval_from_premium_request",
        lambda request, requested_via: {
            "approval_id": "TRADE-999",
            "expires_at": "2026-05-20T07:00:00+00:00",
            "proposal": request["material_payload"]["proposal_payload"],
        },
    )

    result = guarded._resolve_doge_premium_analysis_request(symbol="DOGEUSDT", decision="deny")

    assert result["success"] is True
    assert result["premium_outcome"] == "denied_fallback"
    assert result["trade_approval"]["approval_id"] == "TRADE-999"
    message = guarded._build_doge_premium_resolution_whatsapp_message(result)
    assert "Gemini 3.1 Flash Lite" in message
    assert "Aprobacion requerida TRADE-999" in message


def test_resolve_doge_premium_analysis_request_approved_adjustment_returns_adjust_command(monkeypatch):
    request = {
        "request_id": "PREM-123",
        "status": "pending",
        "symbol": "DOGEUSDT",
        "request_kind": "adjustment",
        "model": "gemini-3.5-flash",
        "material_payload": {
            "position": {"approval_id": "TRADE-123", "market_price": "0.10080"},
            "adjustment_context": {
                "summary": "subir SL para asegurar beneficio",
                "current_stop_price": "0.09950",
                "current_take_profit_price": "0.10100",
                "suggested_stop_price": "0.10020",
                "suggested_take_profit_price": "0.10120",
                "unrealized_pnl_usd": "0.40",
                "unrealized_pnl_pct": "0.80",
                "high_risk": True,
                "high_risk_reason": "el stop sugerido amplia el riesgo",
            },
        },
    }

    monkeypatch.setattr(guarded, "get_latest_doge_premium_analysis_request", lambda **kwargs: request)
    monkeypatch.setattr(
        guarded,
        "record_doge_premium_analysis_decision",
        lambda request_id, **kwargs: {**request, "request_id": request_id, "status": "approved"},
    )
    monkeypatch.setattr(
        guarded,
        "_execute_doge_premium_analysis",
        lambda request: {
            "passed": True,
            "confidence": "0.86",
            "summary": "Gemini 3.5 Flash valida el ajuste.",
            "risk_flags": ["volatilidad alta"],
            "operator_note": "Ajuste valido, pero exige vigilancia.",
            "risk_label": "alto_riesgo",
        },
    )
    monkeypatch.setattr(
        guarded,
        "complete_doge_premium_analysis_request",
        lambda request_id, **kwargs: {
            **request,
            "request_id": request_id,
            "status": "completed",
            "analysis_outcome": "passed",
            "analysis": kwargs.get("analysis"),
        },
    )

    result = guarded._resolve_doge_premium_analysis_request(symbol="DOGEUSDT", decision="approve")

    assert result["success"] is True
    assert result["premium_outcome"] == "passed"
    message = guarded._build_doge_premium_resolution_whatsapp_message(result)
    assert "Gemini 3.5 Flash valida el ajuste" in message
    assert "ALTO RIESGO" in message
    assert "AJUSTAR DOGE" in message