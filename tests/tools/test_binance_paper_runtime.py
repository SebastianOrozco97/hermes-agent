from __future__ import annotations

from decimal import Decimal

from tools.binance_guardrails import BinanceTradeProposal
from tools.binance_paper_runtime import (
    close_paper_position,
    get_paper_account_overview,
    get_paper_position_status,
    open_paper_position,
    reconcile_protective_exits,
    record_market_evidence,
    request_trade_approval,
    record_trade_approval,
    seed_paper_account,
    validate_trade_approval,
)


def _proposal(**overrides):
    payload = {
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
    payload.update(overrides)
    return BinanceTradeProposal.from_payload(payload)


def test_seed_paper_account_sets_persistent_balance(tmp_path):
    overview = seed_paper_account(Decimal("1250"), reset=True, home=tmp_path)

    assert overview["account_snapshot"]["free_balance_usd"] == "1250"
    assert overview["cash_balance_usd"] == "1250"


def test_btc_evidence_requires_two_sources(tmp_path):
    try:
        record_market_evidence(
            symbol="BTCUSDT",
            timeframe="15m",
            binance_reference_price=Decimal("100"),
            external_reference_price=Decimal("101"),
            market_summary="Only one source",
            source_urls=["https://www.binance.com/en/futures/BTCUSDT"],
            home=tmp_path,
        )
    except ValueError as exc:
        assert "at least two source URLs" in str(exc)
    else:
        raise AssertionError("expected multi-source evidence validation to fail")


def test_btc_evidence_requires_real_http_urls(tmp_path):
    try:
        record_market_evidence(
            symbol="BTCUSDT",
            timeframe="15m",
            binance_reference_price=Decimal("100"),
            external_reference_price=Decimal("101"),
            market_summary="Label-only sources should fail",
            source_urls=["Binance Futures Data Feed", "CoinMarketCap BTC Market Data"],
            home=tmp_path,
        )
    except ValueError as exc:
        assert "absolute http(s) URLs" in str(exc)
    else:
        raise AssertionError("expected label-only evidence sources to fail")


def test_btc_evidence_requires_non_binance_confirmation_url(tmp_path):
    try:
        record_market_evidence(
            symbol="BTCUSDT",
            timeframe="15m",
            binance_reference_price=Decimal("100"),
            external_reference_price=Decimal("101"),
            market_summary="Two Binance links should not count as external confirmation",
            source_urls=[
                "https://www.binance.com/en/futures/BTCUSDT",
                "https://www.binance.com/en/price/bitcoin",
            ],
            home=tmp_path,
        )
    except ValueError as exc:
        assert "non-Binance confirmation URL" in str(exc)
    else:
        raise AssertionError("expected external confirmation validation to fail")


def test_trade_approval_must_match_trade_fingerprint(tmp_path):
    evidence = record_market_evidence(
        symbol="BTCUSDT",
        timeframe="15m",
        binance_reference_price=Decimal("100"),
        external_reference_price=Decimal("101"),
        market_summary="Two-source confirmation",
        source_urls=["https://www.binance.com/en/futures/BTCUSDT", "https://www.coingecko.com/en/coins/bitcoin"],
        home=tmp_path,
    )
    approval = request_trade_approval(_proposal(), evidence_id=evidence["evidence_id"], home=tmp_path)
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)

    ok, error, _record = validate_trade_approval(
        approval["approval_id"],
        _proposal(notional_usd="40"),
        home=tmp_path,
    )

    assert ok is False
    assert "does not match" in error


def test_reconcile_protective_exits_closes_take_profit_and_updates_balance(tmp_path):
    seed_paper_account(Decimal("1000"), reset=True, home=tmp_path)
    evidence = record_market_evidence(
        symbol="BTCUSDT",
        timeframe="15m",
        binance_reference_price=Decimal("100"),
        external_reference_price=Decimal("101"),
        market_summary="Two-source confirmation",
        source_urls=["https://www.binance.com/en/futures/BTCUSDT", "https://www.coingecko.com/en/coins/bitcoin"],
        home=tmp_path,
    )
    approval = request_trade_approval(_proposal(), evidence_id=evidence["evidence_id"], home=tmp_path)
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)
    open_paper_position(
        _proposal(),
        reference_price=Decimal("100"),
        approval_id=approval["approval_id"],
        evidence_id=evidence["evidence_id"],
        home=tmp_path,
    )

    reconciled = reconcile_protective_exits(lambda symbol: Decimal("102"), home=tmp_path)
    overview = get_paper_account_overview(home=tmp_path)

    assert len(reconciled["closed_positions"]) == 1
    assert reconciled["closed_positions"][0]["trigger"] == "take_profit"
    assert overview["account_snapshot"]["open_positions"] == 0
    assert overview["cash_balance_usd"] == "1001"


def test_position_status_reports_open_and_closed_snapshots(tmp_path):
    seed_paper_account(Decimal("1000"), reset=True, home=tmp_path)
    approval = request_trade_approval(_proposal(symbol="DOGEUSDT", notional_usd="20", stop_loss_pct="0.5", take_profit_pct="1.0"), home=tmp_path)
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)
    opened = open_paper_position(
        _proposal(symbol="DOGEUSDT", notional_usd="20", stop_loss_pct="0.5", take_profit_pct="1.0"),
        reference_price=Decimal("0.1043"),
        approval_id=approval["approval_id"],
        home=tmp_path,
    )

    open_status = get_paper_position_status(
        approval_id=approval["approval_id"],
        reference_price=Decimal("0.1050"),
        home=tmp_path,
    )

    assert open_status["success"] is True
    assert open_status["status"] == "open"
    assert open_status["market_price"] == "0.105"
    assert open_status["risk"]["estimated_max_loss_usd"] == "0.1"
    assert open_status["risk"]["risk_reward_ratio"] == "2"
    assert open_status["commands"]["close_position"] == f"CERRAR {opened['position']['position_id']}"

    closed = close_paper_position(
        opened["position"]["position_id"],
        exit_price=Decimal("0.105343"),
        reason="take profit reached",
        trigger="take_profit",
        home=tmp_path,
    )
    closed_status = get_paper_position_status(approval_id=approval["approval_id"], home=tmp_path)

    assert closed["duration_seconds"] is not None
    assert closed_status["success"] is True
    assert closed_status["status"] == "closed"
    assert closed_status["trigger"] == "take_profit"
    assert closed_status["realized_pnl_usd"] == closed["realized_pnl_usd"]
    assert closed_status["reason"] == "take profit reached"