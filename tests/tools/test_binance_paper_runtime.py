from __future__ import annotations

import json
from decimal import Decimal

from tools.binance_guardrails import BinanceTradeProposal
from tools.binance_paper_runtime import (
    close_paper_position,
    complete_doge_premium_analysis_request,
    get_latest_doge_premium_analysis_request,
    get_paper_daily_summary,
    get_paper_account_overview,
    get_paper_journal_path,
    get_latest_trade_approval,
    get_paper_position_status,
    open_paper_position,
    reconcile_protective_exits,
    record_doge_premium_analysis_decision,
    record_live_trade_execution_failure,
    record_market_evidence,
    request_doge_premium_analysis,
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


def test_get_latest_trade_approval_returns_most_recent_match(tmp_path):
    record_trade_approval(
        request_trade_approval(_proposal(symbol="DOGEUSDT", notional_usd="20"), home=tmp_path)["approval_id"],
        decision="deny",
        home=tmp_path,
    )
    latest_pending = request_trade_approval(
        _proposal(symbol="DOGEUSDT", notional_usd="21"),
        home=tmp_path,
    )
    request_trade_approval(_proposal(symbol="BTCUSDT", notional_usd="40"), home=tmp_path)

    lookup_pending = get_latest_trade_approval(symbol="DOGEUSDT", status="pending", home=tmp_path)
    lookup_latest = get_latest_trade_approval(symbol="DOGEUSDT", home=tmp_path)

    assert lookup_pending is not None
    assert lookup_pending["approval_id"] == latest_pending["approval_id"]
    assert lookup_latest is not None
    assert lookup_latest["approval_id"] == latest_pending["approval_id"]


def test_get_latest_trade_approval_expires_stale_approved_unconsumed_record(tmp_path):
    stale_approved = {
        "version": 1,
        "approvals": [
            {
                "approval_id": "TRADE-OLD12345",
                "created_at": "2026-05-20T05:00:00+00:00",
                "expires_at": "2026-05-20T05:05:00+00:00",
                "status": "approved",
                "requested_via": "cron_15m_doge",
                "proposal": _proposal(symbol="DOGEUSDT", mode="live").to_dict(),
                "proposal_fingerprint": "ABC123",
                "symbol": "DOGEUSDT",
                "evidence_id": None,
                "market_summary": "stale approved test",
                "source_urls": [],
                "response_text": "approved earlier",
                "decision_by": "operator",
                "decided_at": "2026-05-20T05:01:00+00:00",
                "consumed_at": None,
            }
        ],
    }
    (tmp_path / "binance-trade-approvals.json").write_text(json.dumps(stale_approved), encoding="utf-8")

    latest = get_latest_trade_approval(symbol="DOGEUSDT", home=tmp_path)

    assert latest is not None
    assert latest["approval_id"] == "TRADE-OLD12345"
    assert latest["status"] == "expired"


def test_doge_premium_analysis_request_lifecycle_tracks_status_and_fingerprint(tmp_path):
    request = request_doge_premium_analysis(
        symbol="DOGEUSDT",
        request_kind="entry",
        model="gemini-3.5-flash",
        material_payload={
            "event_kind": "entry",
            "symbol": "DOGEUSDT",
            "signal": {"score": 6, "verdict": "candidate_long"},
        },
        material_summary="entrada premium DOGE",
        home=tmp_path,
    )

    latest = get_latest_doge_premium_analysis_request(symbol="DOGEUSDT", request_kind="entry", home=tmp_path)

    assert latest is not None
    assert latest["request_id"] == request["request_id"]
    assert latest["status"] == "pending"
    assert latest["event_fingerprint"] == request["event_fingerprint"]

    approved = record_doge_premium_analysis_decision(request["request_id"], decision="approve", home=tmp_path)
    completed = complete_doge_premium_analysis_request(
        request["request_id"],
        analysis_outcome="passed",
        analysis={"summary": "Gemini 3.5 Flash confirma la entrada"},
        home=tmp_path,
    )
    by_fingerprint = get_latest_doge_premium_analysis_request(
        symbol="DOGEUSDT",
        request_kind="entry",
        event_fingerprint=request["event_fingerprint"],
        home=tmp_path,
    )

    assert approved["status"] == "approved"
    assert completed["status"] == "completed"
    assert completed["analysis_outcome"] == "passed"
    assert by_fingerprint is not None
    assert by_fingerprint["request_id"] == request["request_id"]


def test_record_live_trade_execution_failure_appends_structured_journal_event(tmp_path):
    record = record_live_trade_execution_failure(
        proposal=_proposal(symbol="DOGEUSDT", mode="live", notional_usd="5.25"),
        error="entry order executed but protective orders failed",
        approval_id="TRADE-FAIL123",
        rollback_sent=True,
        details={"evidence_id": "EVID-123"},
        home=tmp_path,
    )

    journal_lines = get_paper_journal_path(home=tmp_path).read_text(encoding="utf-8").splitlines()
    payload = json.loads(journal_lines[-1])

    assert record["event_type"] == "live_trade_execution_failed"
    assert payload["event_type"] == "live_trade_execution_failed"
    assert payload["approval_id"] == "TRADE-FAIL123"
    assert payload["symbol"] == "DOGEUSDT"
    assert payload["rollback_sent"] is True
    assert payload["details"] == {"evidence_id": "EVID-123"}


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


def test_daily_summary_reports_entries_exits_and_pnl(tmp_path):
    seed_paper_account(Decimal("1000"), reset=True, home=tmp_path)
    approval = request_trade_approval(_proposal(symbol="DOGEUSDT", notional_usd="20", stop_loss_pct="0.5", take_profit_pct="1.0"), home=tmp_path)
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)
    opened = open_paper_position(
        _proposal(symbol="DOGEUSDT", notional_usd="20", stop_loss_pct="0.5", take_profit_pct="1.0"),
        reference_price=Decimal("0.1043"),
        approval_id=approval["approval_id"],
        home=tmp_path,
    )
    close_paper_position(
        opened["position"]["position_id"],
        exit_price=Decimal("0.105343"),
        reason="manual take profit",
        trigger="manual",
        home=tmp_path,
    )

    summary = get_paper_daily_summary(home=tmp_path)

    assert summary["success"] is True
    assert summary["entries_count"] == 1
    assert summary["exits_count"] == 1
    assert summary["approvals_requested"] == 1
    assert summary["approvals_approved"] == 1
    assert summary["approvals_denied"] == 0
    assert summary["open_positions_count"] == 0
    assert summary["realized_pnl_usd"] == "0.2"