from __future__ import annotations

from pathlib import Path

from tools.binance_guardrails import (
    BinanceAccountSnapshot,
    BinanceRiskLimits,
    BinanceTradeProposal,
    evaluate_trade_proposal,
    is_kill_switch_active,
    set_kill_switch,
)


def _valid_proposal(**overrides):
    payload = {
        "symbol": "BTCUSDT",
        "side": "BUY",
        "notional_usd": "125",
        "mode": "paper",
        "order_type": "MARKET",
        "stop_loss_pct": "1.20",
        "take_profit_pct": "2.40",
        "leverage": "1",
        "verifier_model": "gemini-3.1-flash-lite",
        "verifier_passed": True,
        "verifier_confidence": "0.90",
        "dry_run": True,
    }
    payload.update(overrides)
    return BinanceTradeProposal.from_payload(payload)


def _healthy_account(**overrides):
    payload = {
        "free_balance_usd": "1000",
        "open_positions": "0",
        "positions_in_symbol": "0",
        "daily_realized_pnl_usd": "0",
        "kill_switch_active": False,
    }
    payload.update(overrides)
    return BinanceAccountSnapshot.from_payload(payload)


def test_valid_paper_trade_passes_default_limits():
    decision = evaluate_trade_proposal(
        _valid_proposal(),
        _healthy_account(),
        BinanceRiskLimits.from_env({}),
        kill_switch_active=False,
    )

    assert decision.allowed is True
    assert decision.reasons == ()


def test_live_trade_is_blocked_by_default():
    decision = evaluate_trade_proposal(
        _valid_proposal(mode="live"),
        _healthy_account(),
        BinanceRiskLimits.from_env({}),
        kill_switch_active=False,
    )

    assert decision.allowed is False
    assert "live trading is disabled" in " ".join(decision.reasons)


def test_symbol_outside_allowlist_is_blocked():
    limits = BinanceRiskLimits.from_env({"BINANCE_RISK_ALLOWED_SYMBOLS": "BTCUSDT,ETHUSDT"})
    decision = evaluate_trade_proposal(
        _valid_proposal(symbol="DOGEUSDT"),
        _healthy_account(),
        limits,
        kill_switch_active=False,
    )

    assert decision.allowed is False
    assert "outside the allowlist" in " ".join(decision.reasons)


def test_verifier_and_risk_fields_are_mandatory():
    decision = evaluate_trade_proposal(
        _valid_proposal(
            verifier_passed=False,
            verifier_confidence="0.20",
            stop_loss_pct=None,
        ),
        _healthy_account(),
        BinanceRiskLimits.from_env({}),
        kill_switch_active=False,
    )

    joined = " ".join(decision.reasons)
    assert decision.allowed is False
    assert "verifier_passed must be true" in joined
    assert "verifier_confidence is below" in joined
    assert "stop_loss_pct is required" in joined


def test_kill_switch_file_blocks_execution(tmp_path: Path):
    home = tmp_path / ".hermes"
    state = set_kill_switch(True, reason="manual stop", home=home)

    assert state["enabled"] is True
    assert is_kill_switch_active(home=home) is True

    decision = evaluate_trade_proposal(
        _valid_proposal(),
        _healthy_account(),
        BinanceRiskLimits.from_env({}),
        kill_switch_active=is_kill_switch_active(home=home),
    )

    assert decision.allowed is False
    assert "kill switch is active" in decision.reasons


def test_daily_loss_and_position_caps_block_trade():
    limits = BinanceRiskLimits.from_env(
        {
            "BINANCE_RISK_MAX_DAILY_LOSS_USD": "50",
            "BINANCE_RISK_MAX_OPEN_POSITIONS": "1",
            "BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL": "1",
        }
    )
    account = _healthy_account(
        open_positions="1",
        positions_in_symbol="1",
        daily_realized_pnl_usd="-55",
    )

    decision = evaluate_trade_proposal(
        _valid_proposal(),
        account,
        limits,
        kill_switch_active=False,
    )

    joined = " ".join(decision.reasons)
    assert decision.allowed is False
    assert "open_positions" in joined
    assert "positions_in_symbol" in joined
    assert "drawdown" in joined