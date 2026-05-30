from __future__ import annotations

from decimal import Decimal

from tools.doge_strategy_router import build_phase1_overlay_lines, build_strategy_decision_context, build_strategy_digest_lines
from tools.doge_strategy_selector import SelectorFeedbackPolicy, StrategyOpportunity, attach_selector_feedback, select_doge_strategy


def _synthetic_opportunity(
    *,
    strategy_id: str,
    expected_edge: str,
    confidence: str,
    eligible: bool = True,
    macro_alignment: str = "aligned",
    blockers: tuple[str, ...] = (),
    diagnostic_payload: dict[str, object] | None = None,
    primary_regime: str = "unknown",
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
        primary_regime=primary_regime,
    )


def _scorecard_summary() -> dict[str, object]:
    return {
        "start_date": "2026-05-15",
        "end_date": "2026-05-28",
        "strategy_regime_pairs": [
            {
                "strategy_id": "overlay_tactical_long",
                "regime_label": "breakout_trend",
                "sample_count": 8,
                "approvals_requested": 8,
                "approval_conversion_pct": "65",
                "expectancy_usd": "0.14",
                "hit_rate_pct": "62",
                "realized_pnl_usd": "1.12",
            },
            {
                "strategy_id": "funding_arbitrage",
                "regime_label": "funding_rich_carry",
                "sample_count": 8,
                "approvals_requested": 8,
                "approval_conversion_pct": "40",
                "expectancy_usd": "0.02",
                "hit_rate_pct": "50",
                "realized_pnl_usd": "0.16",
            },
        ],
    }


def test_strategy_router_digest_formats_primary_and_alternatives():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.84",
        confidence="0.82",
        primary_regime="breakout_trend",
    )
    arbitrage = _synthetic_opportunity(
        strategy_id="funding_arbitrage",
        expected_edge="0.66",
        confidence="0.69",
        primary_regime="funding_rich_carry",
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
        primary_regime="breakout_trend",
    )
    arbitrage = _synthetic_opportunity(
        strategy_id="funding_arbitrage",
        expected_edge="0.66",
        confidence="0.69",
        primary_regime="funding_rich_carry",
    )

    selection = attach_selector_feedback(
        select_doge_strategy((overlay, arbitrage), conflict_margin=Decimal("0.05")),
        scorecard_summary=_scorecard_summary(),
        policy=SelectorFeedbackPolicy(mode="shadow"),
        conflict_margin=Decimal("0.05"),
    )
    decision_context = build_strategy_decision_context(
        selection,
        macro_state={"risk_level": "normal"},
        verifier_assessments={"gemini_lite": {"confidence": Decimal("0.81")}},
        market_context={"exchange_preview": {"reference_price": Decimal("0.10")}},
    )

    assert decision_context["selected_strategy_id"] == "overlay_tactical_long"
    assert decision_context["selected_strategy"]["holding_horizon"] == "1h"
    assert decision_context["selected_strategy"]["primary_regime"] == "breakout_trend"
    assert decision_context["alternatives_considered"][0]["strategy_id"] == "funding_arbitrage"
    assert decision_context["verifier_assessments"]["gemini_lite"]["confidence"] == "0.81"
    assert decision_context["market_context"]["exchange_preview"]["reference_price"] == "0.1"
    assert decision_context["selector_feedback"]["policy"]["mode"] == "shadow"
    assert decision_context["selector_feedback"]["shadow_selection"]["chosen_strategy_id"] == "overlay_tactical_long"


def test_strategy_router_digest_surfaces_shadow_feedback_when_present():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.70",
        confidence="0.70",
        primary_regime="breakout_trend",
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.68",
        confidence="0.68",
        primary_regime="quiet_range",
    )

    selection = attach_selector_feedback(
        select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.01")),
        scorecard_summary={
            "start_date": "2026-05-15",
            "end_date": "2026-05-28",
            "strategy_regime_pairs": [
                {
                    "strategy_id": "overlay_tactical_long",
                    "regime_label": "breakout_trend",
                    "sample_count": 8,
                    "approvals_requested": 8,
                    "approval_conversion_pct": "25",
                    "expectancy_usd": "-0.12",
                    "hit_rate_pct": "30",
                    "realized_pnl_usd": "-1.20",
                },
                {
                    "strategy_id": "atr_grid",
                    "regime_label": "quiet_range",
                    "sample_count": 8,
                    "approvals_requested": 8,
                    "approval_conversion_pct": "75",
                    "expectancy_usd": "0.20",
                    "hit_rate_pct": "70",
                    "realized_pnl_usd": "1.60",
                },
            ],
        },
        policy=SelectorFeedbackPolicy(mode="shadow"),
        conflict_margin=Decimal("0.01"),
    )
    lines = build_strategy_digest_lines(selection)

    assert any("Shadow feedback: habria priorizado ATR grid" in line for line in lines)


def test_strategy_router_digest_surfaces_phase1_detail_when_overlay_is_blocked():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.28",
        confidence="0.60",
        eligible=False,
        blockers=("signal verdict is standby",),
        primary_regime="quiet_range",
        diagnostic_payload={
            "signal": {
                "timeframe": "15m",
                "signal_score": 2,
                "verdict": "standby",
                "last_close": "0.09973",
                "breakout_reference": "0.10036",
                "ema_fast": "0.09956",
                "ema_slow": "0.09943",
                "rsi_14": "42.50",
                "volume_ratio": "0.60",
            }
        },
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.60",
        confidence="0.70",
        primary_regime="quiet_range",
    )

    selection = select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.05"))
    lines = build_strategy_digest_lines(selection)

    assert any(line.startswith("2. Overlay tactico largo | bloqueada: signal verdict is standby") for line in lines)
    assert any(
        line == (
            "Fase 1 detalle: 15m 2/7 standby @0.09973 | breakout 0.10036 | "
            "EMA9 0.09956 | EMA21 0.09943 | RSI 42.5 | vol 0.6x"
        )
        for line in lines
    )
    assert any(
        line == "Fase 1 gatillo: recuperar breakout 0.10036 con volumen y sostener EMA21 0.09943."
        for line in lines
    )


def test_strategy_router_digest_surfaces_phase1_detail_when_overlay_is_primary():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.84",
        confidence="0.82",
        primary_regime="breakout_trend",
        diagnostic_payload={
            "signal": {
                "timeframe": "15m",
                "signal_score": 6,
                "verdict": "candidate_long",
                "last_close": "0.10072",
                "breakout_reference": "0.10036",
                "ema_fast": "0.10021",
                "ema_slow": "0.09994",
                "rsi_14": "61.40",
                "volume_ratio": "1.34",
            }
        },
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.41",
        confidence="0.58",
        primary_regime="quiet_range",
    )

    selection = select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.05"))
    lines = build_strategy_digest_lines(selection)

    assert lines[0] == "DOGE STRATEGY ROUTER (DOGEUSDT)"
    assert any(line.startswith("Primaria: Overlay tactico largo") for line in lines)
    assert any(
        line == (
            "Fase 1 detalle: 15m 6/7 candidate_long @0.10072 | breakout 0.10036 | "
            "EMA9 0.10021 | EMA21 0.09994 | RSI 61.4 | vol 1.34x"
        )
        for line in lines
    )
    assert any(
        line == "Fase 1 control: breakout 0.10036 ya activo; mientras sostenga EMA21 0.09994, sigue en radar de entrada."
        for line in lines
    )


def test_strategy_router_digest_rounds_high_precision_ema_values():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.28",
        confidence="0.60",
        eligible=False,
        blockers=("signal verdict is standby",),
        primary_regime="quiet_range",
        diagnostic_payload={
            "signal": {
                "timeframe": "15m",
                "signal_score": 6,
                "verdict": "standby",
                "last_close": "0.10119",
                "breakout_reference": "0.10126",
                "ema_fast": "0.1010098642597475882747662996",
                "ema_slow": "0.1009375854250025288516408667",
                "rsi_14": "59.70",
                "volume_ratio": "2.32",
            }
        },
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.60",
        confidence="0.70",
        primary_regime="quiet_range",
    )

    selection = select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.05"))
    lines = build_strategy_digest_lines(selection)

    assert any(
        line == (
            "Fase 1 detalle: 15m 6/7 standby @0.10119 | breakout 0.10126 | "
            "EMA9 0.10101 | EMA21 0.10094 | RSI 59.7 | vol 2.32x"
        )
        for line in lines
    )
    assert any(
        line == "Fase 1 gatillo: recuperar breakout 0.10126 con volumen y sostener EMA21 0.10094."
        for line in lines
    )


def test_build_phase1_overlay_lines_surfaces_router_priority_and_blocker():
    overlay = _synthetic_opportunity(
        strategy_id="overlay_tactical_long",
        expected_edge="0.28",
        confidence="0.60",
        eligible=False,
        blockers=("signal verdict is standby",),
        primary_regime="quiet_range",
        diagnostic_payload={
            "signal": {
                "timeframe": "15m",
                "signal_score": 2,
                "verdict": "standby",
                "last_close": "0.09973",
                "breakout_reference": "0.10036",
                "ema_fast": "0.09956",
                "ema_slow": "0.09943",
                "rsi_14": "42.50",
                "volume_ratio": "0.60",
            }
        },
    )
    grid = _synthetic_opportunity(
        strategy_id="atr_grid",
        expected_edge="0.60",
        confidence="0.70",
        primary_regime="quiet_range",
    )

    selection = select_doge_strategy((overlay, grid), conflict_margin=Decimal("0.05"))
    lines = build_phase1_overlay_lines(selection)

    assert lines[0] == "FASE 1: OVERLAY TACTICO (DOGEUSDT)"
    assert any(line.startswith("Estado: en espera | macro aligned | regimen quiet_range") for line in lines)
    assert any(line.startswith("Fase 1 detalle: 15m 2/7 standby @0.09973") for line in lines)
    assert any(line == "Prioridad router: ATR grid -> enter." for line in lines)
    assert any(line == "Fase 1 bloqueo actual: signal verdict is standby." for line in lines)