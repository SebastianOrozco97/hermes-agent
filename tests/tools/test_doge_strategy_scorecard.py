from __future__ import annotations

from decimal import Decimal

from tools.binance_guardrails import BinanceTradeProposal
from tools.binance_paper_runtime import (
    close_paper_position,
    open_paper_position,
    record_trade_approval,
    request_trade_approval,
    seed_paper_account,
)
from tools.doge_strategy_scorecard import (
    build_doge_strategy_scorecard_lines,
    get_doge_strategy_daily_scorecard,
    get_doge_strategy_weekly_scorecard,
)


def _proposal(**overrides):
    payload = {
        "symbol": "DOGEUSDT",
        "side": "BUY",
        "notional_usd": "20",
        "mode": "paper",
        "order_type": "MARKET",
        "stop_loss_pct": "0.5",
        "take_profit_pct": "1.0",
        "leverage": "1",
        "verifier_model": "gemini-3.1-flash-lite",
        "verifier_passed": True,
        "verifier_confidence": "0.95",
        "dry_run": False,
    }
    payload.update(overrides)
    return BinanceTradeProposal.from_payload(payload)


def _decision_context(strategy_id: str, regime_tags: tuple[str, ...], primary_regime: str = "") -> dict[str, object]:
    return {
        "selector_family": "doge_meta_selector_v1",
        "selected_strategy_id": strategy_id,
        "selected_strategy": {
            "strategy_id": strategy_id,
            "expected_edge": "0.83",
            "confidence": "0.80",
            "capital_required_usd": "20",
            "holding_horizon": "30-90m",
            "macro_alignment": "aligned",
            "primary_regime": primary_regime,
            "regime_tags": list(regime_tags),
        },
        "alternatives_considered": [],
        "macro_state": {
            "risk_level": "normal",
            "btc_trend_1h": "bullish",
            "btc_trend_4h": "bullish",
        },
        "market_context": {},
    }


def test_doge_strategy_daily_and_weekly_scorecards_filter_to_doge_and_build_lines(tmp_path):
    seed_paper_account(Decimal("1000"), reset=True, home=tmp_path)

    approval = request_trade_approval(
        _proposal(symbol="DOGEUSDT"),
        decision_context=_decision_context(
            "overlay_tactical_long",
            ("directional_overlay", "breakout_pressure"),
            primary_regime="breakout_trend",
        ),
        home=tmp_path,
    )
    record_trade_approval(approval["approval_id"], decision="approve", home=tmp_path)

    doge_position = open_paper_position(
        _proposal(symbol="DOGEUSDT"),
        reference_price=Decimal("0.1043"),
        approval_id=approval["approval_id"],
        decision_context=_decision_context(
            "overlay_tactical_long",
            ("directional_overlay", "breakout_pressure"),
            primary_regime="breakout_trend",
        ),
        home=tmp_path,
    )
    close_paper_position(
        doge_position["position"]["position_id"],
        exit_price=Decimal("0.105343"),
        reason="take profit reached",
        trigger="take_profit",
        thesis_outcome="validated",
        home=tmp_path,
    )

    btc_position = open_paper_position(
        _proposal(symbol="BTCUSDT", notional_usd="50", stop_loss_pct="1.0", take_profit_pct="2.0"),
        reference_price=Decimal("100"),
        approval_id="TRADE-BTC",
        decision_context=_decision_context(
            "funding_arbitrage",
            ("funding_carry", "delta_neutral"),
            primary_regime="funding_rich_carry",
        ),
        home=tmp_path,
    )
    close_paper_position(
        btc_position["position"]["position_id"],
        exit_price=Decimal("101"),
        reason="carry harvested",
        trigger="manual",
        thesis_outcome="managed_profit",
        home=tmp_path,
    )

    daily = get_doge_strategy_daily_scorecard(home=tmp_path)
    weekly = get_doge_strategy_weekly_scorecard(home=tmp_path)
    lines = build_doge_strategy_scorecard_lines(daily)

    assert daily["symbol"] == "DOGEUSDT"
    assert daily["total_matches"] == 1
    assert daily["approval_conversion_pct"] == "100"
    assert daily["strategy_regime_pairs"][0]["regime_label"] == "breakout_trend"
    assert weekly["total_matches"] == 1
    assert lines[0].startswith("DOGE scorecard")
    assert "conv 100%" in lines[1]
    assert any("Pareja lider" in line for line in lines)
    assert any("Overlay tactico largo" in line for line in lines)