from __future__ import annotations

from decimal import Decimal

from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage
from tools.doge_grid_advisor import plan_dynamic_grid
from tools.doge_regime_classifier import (
    classify_arbitrage_regime,
    classify_grid_regime,
    classify_no_trade_regime,
    classify_overlay_regime,
)
from tools.doge_signal_engine import DogeSignalSnapshot


def _overlay_signal(*, volume_ratio: str = "1.30", score: int = 6) -> DogeSignalSnapshot:
    return DogeSignalSnapshot(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1015"),
        ema_fast=Decimal("0.1008"),
        previous_ema_fast=Decimal("0.1004"),
        ema_slow=Decimal("0.1000"),
        previous_ema_slow=Decimal("0.0998"),
        rsi_14=Decimal("62"),
        volume_ratio=Decimal(volume_ratio),
        breakout_reference=Decimal("0.1010"),
        signal_score=score,
        verifier_confidence=Decimal("0.82"),
        verdict="candidate_long",
        rationale="breakout remains intact",
        market_summary="DOGE pushes above local highs",
    )


def test_overlay_regime_classifies_breakout_trend():
    classification = classify_overlay_regime(_overlay_signal(), macro_alignment="aligned")

    assert classification.primary_regime == "breakout_trend"
    assert "breakout_pressure" in classification.regime_tags
    assert "volume_confirmed" in classification.regime_tags


def test_grid_regime_classifies_quiet_range():
    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.003"),
    )

    classification = classify_grid_regime(plan, macro_alignment="aligned")

    assert classification.primary_regime == "quiet_range"
    assert "range_bound" in classification.regime_tags


def test_grid_regime_classifies_high_volatility_stress():
    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.010"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.003"),
    )

    classification = classify_grid_regime(
        plan,
        macro_alignment="blocked",
        macro_state={"risk_level": "high_volatility", "btc_trend_1h": "bearish", "btc_trend_4h": "bearish"},
    )

    assert classification.primary_regime == "high_volatility_stress"


def test_arbitrage_regime_classifies_funding_rich_carry():
    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    classification = classify_arbitrage_regime(plan, macro_alignment="aligned")

    assert classification.primary_regime == "funding_rich_carry"
    assert "delta_neutral" in classification.regime_tags


def test_no_trade_regime_classifies_macro_divergent_chop():
    classification = classify_no_trade_regime(
        blockers=("macro divergence blocks the next DOGE entry",),
        macro_alignment="divergent",
    )

    assert classification.primary_regime == "macro_divergent_chop"