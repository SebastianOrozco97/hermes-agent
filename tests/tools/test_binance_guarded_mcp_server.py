from __future__ import annotations

import os
from decimal import Decimal

import agent.transports.binance_guarded_mcp_server as guarded
from tools.binance_paper_runtime import record_market_evidence, request_trade_approval, record_trade_approval


class _FakeExecutor:
    def __init__(self):
        self.submit_calls = []

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

    assert "PnL 0.2 USD (1%)" in message
    assert "Motivo: protective exit triggered via take_profit" in message
    assert "Duracion 4m 10s" in message
    assert "Seguimiento: ESTADO TRADE-123" in message


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