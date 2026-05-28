from __future__ import annotations

from decimal import Decimal

from tools.doge_strategy_router import build_strategy_decision_context, build_strategy_digest_lines
from tools.doge_strategy_selector import StrategyOpportunity, select_doge_strategy


def _synthetic_opportunity(
    *,
    strategy_id: str,
    expected_edge: str,
    confidence: str,
    eligible: bool = True,
    macro_alignment: str = "aligned",
    blockers: tuple[str, ...] = (),
    diagnostic_payload: dict[str, object] | None = None,
) -> StrategyOpportunity:
    return StrategyOpportunity(
        strategy_id=strategy_id,
        symbol="DOGEUSDT",
        action="enter" if eligible else "hold",
        eligible=eligible,
        blockers=blockers,
        expected_edge=Decimal(expected_edge),
        confidence=Decimal(confidence),
        capital_required_usd=Decimal("5"),
        holding_horizon="1h",
        macro_alignment=macro_alignment,
        regime_tags=(strategy_id,),
        operator_summary=f"{strategy_id} summary",
        diagnostic_payload=diagnostic_payload or {},
    )


def test_strategy_router_digest_formats_primary_and_alternatives():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.84",
        confidence="0.82",
    )
    arbitrage = _synthetic_opportunity(
        strategy_id="funding_arbitrage",
        expected_edge="0.66",
        confidence="0.69",
    )

    selection = select_doge_strategy((overlay, arbitrage), conflict_margin=Decimal("0.05"))
    lines = build_strategy_digest_lines(selection)

    assert lines[0] == "DOGE STRATEGY ROUTER (DOGEUSDT)"
    assert "Primaria: Overlay tactico largo -> enter." in lines
    assert "Alternativas:" in lines
    assert any(line.startswith("2. Arbitraje de funding") for line in lines)
    assert lines[-1] == "Diagnosticos: doge_live_scout.py | doge_arbitrage_scout.py | doge_grid_scout.py"


def test_strategy_router_digest_formats_abstention_reason():
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
    lines = build_strategy_digest_lines(selection)

    assert selection.abstained is True
    assert "Primaria: NO TRADE." in lines
    assert any(line.startswith("Abstencion: conflicting opportunities") for line in lines)
    assert any(line.startswith("1. Overlay tactico largo") for line in lines)


def test_strategy_router_decision_context_serializes_selection_and_verifiers():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.84",
        confidence="0.82",
    )
    arbitrage = _synthetic_opportunity(
        strategy_id="funding_arbitrage",
        expected_edge="0.66",
        confidence="0.69",
    )

    selection = select_doge_strategy((overlay, arbitrage), conflict_margin=Decimal("0.05"))
    decision_context = build_strategy_decision_context(
        selection,
        macro_state={"risk_level": "normal"},
        verifier_assessments={"gemini_lite": {"confidence": Decimal("0.81")}},
        market_context={"exchange_preview": {"reference_price": Decimal("0.10")}},
    )

    assert decision_context["selected_strategy_id"] == "overlay_tactical_long"
    assert decision_context["selected_strategy"]["holding_horizon"] == "1h"
    assert decision_context["alternatives_considered"][0]["strategy_id"] == "funding_arbitrage"
    assert decision_context["verifier_assessments"]["gemini_lite"]["confidence"] == "0.81"
    assert decision_context["market_context"]["exchange_preview"]["reference_price"] == "0.1"