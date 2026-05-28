from __future__ import annotations

from decimal import Decimal

import pytest

from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage
from tools.doge_grid_advisor import plan_dynamic_grid
from tools.doge_signal_engine import DogeSignalSnapshot
from tools.doge_strategy_selector import (
    StrategyOpportunity,
    arbitrage_opportunity_from_plan,
    build_no_trade_opportunity,
    grid_opportunity_from_plan,
    overlay_opportunity_from_signal,
    select_doge_strategy,
)


def _overlay_signal(*, verdict: str, score: int, volume_ratio: str = "1.20") -> DogeSignalSnapshot:
    return DogeSignalSnapshot(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1010"),
        ema_fast=Decimal("0.1005"),
        previous_ema_fast=Decimal("0.1002"),
        ema_slow=Decimal("0.0998"),
        previous_ema_slow=Decimal("0.0996"),
        rsi_14=Decimal("58"),
        volume_ratio=Decimal(volume_ratio),
        breakout_reference=Decimal("0.1008"),
        signal_score=score,
        verifier_confidence=Decimal("0.81"),
        verdict=verdict,
        rationale="DOGE breakout structure remains intact.",
        market_summary="DOGE is pressing through the recent local range.",
    )


def _synthetic_opportunity(
    *,
    strategy_id: str,
    eligible: bool = True,
    expected_edge: str = "0.60",
    confidence: str = "0.70",
    capital_required_usd: str = "5",
    macro_alignment: str = "aligned",
    blockers: tuple[str, ...] = (),
    diagnostic_payload: dict[str, object] | None = None,
) -> StrategyOpportunity:
    return StrategyOpportunity(
        strategy_id=strategy_id,
        symbol="DOGEUSDT",
        action="act" if eligible else "hold",
        eligible=eligible,
        blockers=blockers,
        expected_edge=Decimal(expected_edge),
        confidence=Decimal(confidence),
        capital_required_usd=Decimal(capital_required_usd),
        holding_horizon="1h",
        macro_alignment=macro_alignment,
        regime_tags=(strategy_id,),
        operator_summary=f"{strategy_id} summary",
        diagnostic_payload=diagnostic_payload or {},
    )


def test_overlay_opportunity_maps_candidate_signal():
    opportunity = overlay_opportunity_from_signal(
        _overlay_signal(verdict="candidate_long", score=6),
        notional_usd=Decimal("5.25"),
        macro_alignment="aligned",
    )

    assert opportunity.eligible is True
    assert opportunity.strategy_id == "overlay_tactical_long"
    assert opportunity.action == "enter_long"
    assert opportunity.expected_edge == pytest.approx(Decimal("0.8571428571428571428571428571"))
    assert opportunity.confidence == Decimal("0.81")
    assert opportunity.capital_required_usd == Decimal("5.25")
    assert "breakout_pressure" in opportunity.regime_tags
    assert "volume_confirmed" in opportunity.regime_tags
    assert opportunity.macro_alignment == "aligned"


def test_overlay_opportunity_blocks_standby_signal():
    opportunity = overlay_opportunity_from_signal(
        _overlay_signal(verdict="standby", score=4, volume_ratio="0.95"),
        notional_usd=Decimal("5.25"),
        macro_alignment="divergent",
    )

    assert opportunity.eligible is False
    assert opportunity.action == "hold"
    assert "signal verdict is standby" in opportunity.blockers
    assert opportunity.macro_alignment == "divergent"


def test_arbitrage_opportunity_maps_entry_plan():
    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0020"),
    )

    opportunity = arbitrage_opportunity_from_plan(plan, macro_alignment="aligned")

    assert opportunity.eligible is True
    assert opportunity.strategy_id == "funding_arbitrage"
    assert opportunity.action == "enter_arbitrage"
    assert opportunity.capital_required_usd == pytest.approx(Decimal("10"))
    assert opportunity.diagnostic_payload["expected_yield_pct"] == "0.2"
    assert "delta_neutral" in opportunity.regime_tags


def test_arbitrage_opportunity_blocks_hold_plan():
    plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0002"),
    )

    opportunity = arbitrage_opportunity_from_plan(plan, macro_alignment="blocked")

    assert opportunity.eligible is False
    assert opportunity.action == "hold"
    assert "funding is below the arbitrage entry threshold" in opportunity.blockers
    assert opportunity.macro_alignment == "blocked"


def test_grid_opportunity_maps_range_regime():
    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.003"),
    )

    opportunity = grid_opportunity_from_plan(plan, macro_alignment="aligned")

    assert opportunity.eligible is True
    assert opportunity.strategy_id == "atr_grid"
    assert opportunity.action == "seed_grid"
    assert opportunity.expected_edge == Decimal("0.60")
    assert opportunity.confidence == Decimal("0.70")
    assert "range_bound" in opportunity.regime_tags


def test_grid_opportunity_blocks_trend_regime():
    plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.03"),
    )

    opportunity = grid_opportunity_from_plan(plan, macro_alignment="aligned")

    assert opportunity.eligible is False
    assert opportunity.action == "hold"
    assert plan.regime_reason in opportunity.blockers
    assert opportunity.expected_edge == Decimal("0.18")


def test_build_no_trade_opportunity_requires_blocker_and_zero_capital():
    opportunity = build_no_trade_opportunity(
        symbol="DOGEUSDT",
        blockers=["macro and regime are both hostile"],
        macro_alignment="blocked",
        regime_tags=["high_volatility_stress"],
        operator_summary="DOGE should stay flat until conditions improve.",
    )

    assert isinstance(opportunity, StrategyOpportunity)
    assert opportunity.strategy_id == "no_trade"
    assert opportunity.eligible is False
    assert opportunity.capital_required_usd == Decimal("0")
    assert opportunity.confidence == Decimal("1")
    assert opportunity.blockers == ("macro and regime are both hostile",)
    assert opportunity.to_dict()["diagnostic_payload"]["blocker_count"] == 1


def test_selector_prefers_overlay_over_grid_when_score_is_higher():
    overlay = overlay_opportunity_from_signal(
        _overlay_signal(verdict="candidate_long", score=6),
        notional_usd=Decimal("5.25"),
        macro_alignment="aligned",
    )
    grid_plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.003"),
    )
    grid = grid_opportunity_from_plan(grid_plan, macro_alignment="aligned")

    selection = select_doge_strategy((grid, overlay))

    assert selection.abstained is False
    assert selection.chosen_strategy_id == "overlay_tactical_long"
    assert selection.ranked_opportunities[0].opportunity.strategy_id == "overlay_tactical_long"
    assert selection.rejected_alternatives[0].opportunity.strategy_id == "atr_grid"


def test_selector_prefers_arbitrage_over_weaker_overlay():
    overlay = overlay_opportunity_from_signal(
        _overlay_signal(verdict="candidate_long", score=4),
        notional_usd=Decimal("5.25"),
        macro_alignment="aligned",
    )
    arbitrage_plan = plan_delta_neutral_arbitrage(
        symbol="DOGEUSDT",
        available_capital_usd=Decimal("10"),
        market_price=Decimal("0.10"),
        funding_rate=Decimal("0.0030"),
    )
    arbitrage = arbitrage_opportunity_from_plan(arbitrage_plan, macro_alignment="aligned")

    selection = select_doge_strategy((overlay, arbitrage))

    assert selection.abstained is False
    assert selection.chosen_strategy_id == "funding_arbitrage"
    assert selection.ranked_opportunities[0].opportunity.strategy_id == "funding_arbitrage"


def test_selector_abstains_when_top_strategies_are_too_close():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.70",
        confidence="0.70",
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.69",
        confidence="0.70",
    )

    selection = select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.08"))

    assert selection.abstained is True
    assert selection.chosen_strategy_id == "no_trade"
    assert "conflicting opportunities" in selection.abstain_reason
    assert selection.chosen_opportunity.blockers == (selection.abstain_reason,)


def test_selector_abstains_when_macro_blocker_forces_fail_closed():
    overlay = overlay_opportunity_from_signal(
        _overlay_signal(verdict="candidate_long", score=6),
        notional_usd=Decimal("5.25"),
        macro_alignment="blocked",
    )
    grid_plan = plan_dynamic_grid(
        symbol="DOGEUSDT",
        market_price=Decimal("0.10"),
        atr=Decimal("0.001"),
        available_capital=Decimal("20"),
        grids_per_side=3,
        trend_bias_pct=Decimal("0.03"),
    )
    grid = grid_opportunity_from_plan(grid_plan, macro_alignment="aligned")

    selection = select_doge_strategy((overlay, grid))

    assert selection.abstained is True
    assert selection.chosen_strategy_id == "no_trade"
    assert selection.abstain_reason == "every strategy lane is blocked or incomplete"
    assert "overlay_tactical_long: macro alignment is blocked" in selection.chosen_opportunity.blockers


def test_selector_abstains_when_sample_size_gate_is_missing():
    sample_limited = _synthetic_opportunity(
        strategy_id="funding_arbitrage",
        diagnostic_payload={"sample_size_ready": False},
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        eligible=False,
        blockers=("trend regime blocks the grid",),
    )

    selection = select_doge_strategy((sample_limited, grid))

    assert selection.abstained is True
    assert selection.chosen_strategy_id == "no_trade"
    assert "funding_arbitrage: sample size gate is not ready" in selection.chosen_opportunity.blockers