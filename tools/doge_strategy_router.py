from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping

from tools.binance_guardrails import get_strategy_leverage_cap
from tools.binance_live_adapter import BinanceFuturesLiveExecutor
from tools.doge_strategy_scorecard import get_doge_strategy_scorecard
from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage
from tools.doge_grid_advisor import plan_dynamic_grid
from tools.doge_signal_engine import DogeSignalSnapshot, _atr, analyze_doge_15m_signal, parse_binance_klines
from tools.doge_strategy_selector import (
    RankedOpportunity,
    SelectorFeedbackPolicy,
    StrategyOpportunity,
    StrategySelection,
    attach_selector_feedback,
    arbitrage_opportunity_from_plan,
    grid_opportunity_from_plan,
    overlay_opportunity_from_signal,
    select_doge_strategy,
)


_STRATEGY_LABELS = {
    "overlay_tactical_long": "Overlay tactico largo",
    "funding_arbitrage": "Arbitraje de funding",
    "atr_grid": "ATR grid",
    "no_trade": "No trade",
}

_PRICE_DISPLAY_QUANTUM = Decimal("0.00001")


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _price_text(value: Decimal) -> str:
    return _decimal_text(value.quantize(_PRICE_DISPLAY_QUANTUM))


def strategy_label(strategy_id: str) -> str:
    normalized = str(strategy_id or "").strip()
    if not normalized:
        return "Desconocida"
    return _STRATEGY_LABELS.get(normalized, normalized.replace("_", " ").title())


def _ranked_line(ranked: RankedOpportunity) -> str:
    opportunity = ranked.opportunity
    label = strategy_label(opportunity.strategy_id)
    if ranked.eligible_for_selection:
        return (
            f"{ranked.rank}. {label} | score {_decimal_text(ranked.selection_score)} | "
            f"edge {_decimal_text(opportunity.expected_edge)} | conf {_decimal_text(opportunity.confidence)}"
        )
    reason = ranked.rejection_reason or "; ".join(opportunity.blockers) or "sin detalle"
    return f"{ranked.rank}. {label} | bloqueada: {reason}"


def _coerce_decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _overlay_detail_lines(opportunity: StrategyOpportunity) -> list[str]:
    if opportunity.strategy_id != "overlay_tactical_long":
        return []

    signal_payload = opportunity.diagnostic_payload.get("signal")
    if not isinstance(signal_payload, Mapping):
        return []

    timeframe = str(signal_payload.get("timeframe") or "15m").strip() or "15m"
    verdict = str(signal_payload.get("verdict") or "unknown").strip() or "unknown"
    score = str(signal_payload.get("signal_score") or "n/d").strip() or "n/d"

    last_close = _coerce_decimal(signal_payload.get("last_close"))
    breakout_reference = _coerce_decimal(signal_payload.get("breakout_reference"))
    ema_fast = _coerce_decimal(signal_payload.get("ema_fast"))
    ema_slow = _coerce_decimal(signal_payload.get("ema_slow"))
    rsi_14 = _coerce_decimal(signal_payload.get("rsi_14"))
    volume_ratio = _coerce_decimal(signal_payload.get("volume_ratio"))

    price_text = _decimal_text(last_close) if last_close is not None else "n/d"
    breakout_text = _decimal_text(breakout_reference) if breakout_reference is not None else "n/d"
    ema_fast_text = _price_text(ema_fast) if ema_fast is not None else "n/d"
    ema_slow_text = _price_text(ema_slow) if ema_slow is not None else "n/d"
    rsi_text = _decimal_text(rsi_14.quantize(Decimal("0.01"))) if rsi_14 is not None else "n/d"
    volume_text = _decimal_text(volume_ratio.quantize(Decimal("0.01"))) if volume_ratio is not None else "n/d"

    lines = [
        (
            f"Fase 1 detalle: {timeframe} {score}/7 {verdict} @{price_text} | "
            f"breakout {breakout_text} | EMA9 {ema_fast_text} | EMA21 {ema_slow_text} | "
            f"RSI {rsi_text} | vol {volume_text}x"
        )
    ]
    if verdict == "candidate_long":
        lines.append(
            f"Fase 1 control: breakout {breakout_text} ya activo; mientras sostenga EMA21 {ema_slow_text}, sigue en radar de entrada."
        )
    else:
        lines.append(
            f"Fase 1 gatillo: recuperar breakout {breakout_text} con volumen y sostener EMA21 {ema_slow_text}."
        )
    return lines


def _find_ranked_opportunity(selection: StrategySelection, strategy_id: str) -> RankedOpportunity | None:
    for ranked in selection.ranked_opportunities:
        if ranked.opportunity.strategy_id == strategy_id:
            return ranked
    return None


def build_phase1_overlay_lines(selection: StrategySelection) -> list[str]:
    ranked_overlay = _find_ranked_opportunity(selection, "overlay_tactical_long")
    if ranked_overlay is None:
        return [f"FASE 1: sin lectura overlay disponible ({selection.symbol})."]

    overlay = ranked_overlay.opportunity
    lines = [f"FASE 1: OVERLAY TACTICO ({overlay.symbol})"]
    lines.append(
        "Estado: "
        f"{'lista' if overlay.eligible else 'en espera'} | "
        f"macro {overlay.macro_alignment} | regimen {overlay.primary_regime} | "
        f"horizonte {overlay.holding_horizon} | edge {_decimal_text(overlay.expected_edge)} | "
        f"confianza {_decimal_text(overlay.confidence)}"
    )

    detail_lines = _overlay_detail_lines(overlay)
    if detail_lines:
        lines.extend(detail_lines)
    else:
        lines.append(f"Tesis overlay: {overlay.operator_summary}")

    if selection.abstained:
        reason = selection.abstain_reason or ranked_overlay.rejection_reason or "; ".join(overlay.blockers) or "sin detalle"
        lines.append(f"Prioridad router: NO TRADE. {reason}.")
    elif selection.chosen_strategy_id == "overlay_tactical_long":
        lines.append("Prioridad router: Fase 1 es la estrategia primaria en este ciclo.")
    else:
        reason = ranked_overlay.rejection_reason or "; ".join(overlay.blockers) or "sin detalle"
        lines.append(
            f"Prioridad router: {strategy_label(selection.chosen_strategy_id)} -> {selection.chosen_opportunity.action}."
        )
        lines.append(f"Fase 1 bloqueo actual: {reason}.")
    return lines


def build_strategy_digest_lines(selection: StrategySelection) -> list[str]:
    chosen = selection.chosen_opportunity
    lines = [f"DOGE STRATEGY ROUTER ({selection.symbol})"]

    if selection.abstained:
        lines.append("Primaria: NO TRADE.")
        lines.append(f"Abstencion: {selection.abstain_reason}.")
    else:
        lines.append(f"Primaria: {strategy_label(chosen.strategy_id)} -> {chosen.action}.")
        lines.append(f"Tesis: {chosen.operator_summary}")
        lines.extend(_overlay_detail_lines(chosen))

    lines.append(
        "Marco: "
        f"macro {chosen.macro_alignment} | horizonte {chosen.holding_horizon} | "
        f"capital {_decimal_text(chosen.capital_required_usd)} USD | "
        f"edge {_decimal_text(chosen.expected_edge)} | confianza {_decimal_text(chosen.confidence)}"
    )

    alternatives = list(selection.rejected_alternatives)
    if alternatives:
        lines.append("Alternativas:")
        for ranked in alternatives:
            lines.append(_ranked_line(ranked))
            lines.extend(_overlay_detail_lines(ranked.opportunity))

    feedback_result = selection.feedback_result
    if feedback_result is not None and feedback_result.policy.resolved_mode == "shadow":
        if feedback_result.shadow_abstained:
            lines.append(f"Shadow feedback: habria NO TRADE. {feedback_result.shadow_abstain_reason}.")
        elif feedback_result.shadow_would_change_selection:
            lines.append(
                "Shadow feedback: habria priorizado "
                f"{strategy_label(feedback_result.shadow_chosen_strategy_id)} usando evidencia historica por estrategia x regimen."
            )
        elif any(item.policy_action == "insufficient_sample" for item in feedback_result.evaluations):
            lines.append("Shadow feedback: aun no hay muestra suficiente para cambiar la prioridad del selector.")
        else:
            lines.append("Shadow feedback: mantiene la misma prioridad con la evidencia historica reciente.")

    lines.append("Diagnosticos: doge_live_scout.py | doge_arbitrage_scout.py | doge_grid_scout.py")
    return lines


def _normalize_json_payload(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_text(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _opportunity_snapshot(opportunity: StrategyOpportunity) -> dict[str, Any]:
    return {
        "strategy_id": opportunity.strategy_id,
        "action": opportunity.action,
        "eligible": opportunity.eligible,
        "expected_edge": _decimal_text(opportunity.expected_edge),
        "confidence": _decimal_text(opportunity.confidence),
        "capital_required_usd": _decimal_text(opportunity.capital_required_usd),
        "holding_horizon": opportunity.holding_horizon,
        "macro_alignment": opportunity.macro_alignment,
        "primary_regime": opportunity.primary_regime,
        "regime_tags": list(opportunity.regime_tags),
        "blockers": list(opportunity.blockers),
        "operator_summary": opportunity.operator_summary,
    }


def build_strategy_decision_context(
    selection: StrategySelection,
    *,
    macro_state: Mapping[str, Any] | None = None,
    verifier_assessments: Mapping[str, Any] | None = None,
    market_context: Mapping[str, Any] | None = None,
    execution_request: Mapping[str, Any] | None = None,
    selector_family: str = "doge_meta_selector_v1",
) -> dict[str, Any]:
    alternatives: list[dict[str, Any]] = []
    for ranked in selection.rejected_alternatives:
        payload = _opportunity_snapshot(ranked.opportunity)
        payload.update(
            {
                "rank": ranked.rank,
                "selection_score": _decimal_text(ranked.selection_score),
                "eligible_for_selection": ranked.eligible_for_selection,
                "rejection_reason": ranked.rejection_reason,
            }
        )
        alternatives.append(payload)

    return {
        "selector_family": selector_family,
        "selected_strategy_id": selection.chosen_strategy_id,
        "selected_strategy": _opportunity_snapshot(selection.chosen_opportunity),
        "alternatives_considered": alternatives,
        "selector_outcome": {
            "abstained": selection.abstained,
            "abstain_reason": selection.abstain_reason,
        },
        "macro_state": _normalize_json_payload(macro_state or {}),
        "verifier_assessments": _normalize_json_payload(verifier_assessments or {}),
        "market_context": _normalize_json_payload(market_context or {}),
        "execution_request": _normalize_json_payload(execution_request or {}),
        "selector_feedback": selection.feedback_result.to_dict() if selection.feedback_result is not None else None,
    }


def _default_feedback_policy() -> SelectorFeedbackPolicy:
    return SelectorFeedbackPolicy(mode="shadow")


def _build_overlay_opportunity(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    timeframe: str,
    notional_usd: Decimal,
    min_signal_score: int,
    macro_alignment: str,
    overlay_signal: DogeSignalSnapshot | None = None,
) -> StrategyOpportunity:
    signal = overlay_signal
    if signal is None:
        raw_klines = executor.get_klines(symbol, interval=timeframe, limit=120)
        closed_klines = raw_klines[:-1] if len(raw_klines) > 1 else raw_klines
        signal = analyze_doge_15m_signal(
            parse_binance_klines(closed_klines),
            score_threshold=min_signal_score,
            timeframe=timeframe,
        )
    return overlay_opportunity_from_signal(signal, notional_usd=notional_usd, macro_alignment=macro_alignment)


def _build_arbitrage_opportunity(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    capital_usd: Decimal,
    base_macro_alignment: str,
) -> StrategyOpportunity:
    premium_info = executor._request("GET", "/fapi/v1/premiumIndex", params={"symbol": symbol})
    funding_rate = Decimal(str(premium_info.get("lastFundingRate", "0")))
    market_price = executor.get_reference_price(symbol)
    plan = plan_delta_neutral_arbitrage(
        symbol=symbol,
        available_capital_usd=capital_usd,
        market_price=market_price,
        funding_rate=funding_rate,
        leverage=get_strategy_leverage_cap("arbitrage"),
    )
    macro_alignment = "cautious" if base_macro_alignment in {"blocked", "divergent"} else "aligned"
    return arbitrage_opportunity_from_plan(plan, macro_alignment=macro_alignment)


def _build_grid_opportunity(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    capital_usd: Decimal,
    base_macro_alignment: str,
) -> StrategyOpportunity:
    market_price = executor.get_reference_price(symbol)
    klines = executor._request("GET", "/fapi/v1/klines", params={"symbol": symbol, "interval": "1h", "limit": 40})
    candles = parse_binance_klines(klines)
    atr_value = _atr(candles, period=14)
    trend_bias_pct = Decimal("0")
    if len(candles) >= 12 and candles[0].close > 0:
        trend_bias_pct = (candles[-1].close - candles[0].close) / candles[0].close

    plan = plan_dynamic_grid(
        symbol=symbol,
        market_price=market_price,
        atr=atr_value,
        available_capital=capital_usd,
        grids_per_side=3,
        atr_multiplier=Decimal("1.5"),
        trend_bias_pct=trend_bias_pct,
        leverage=get_strategy_leverage_cap("grid"),
    )
    macro_alignment = "blocked" if base_macro_alignment == "blocked" else "cautious"
    if base_macro_alignment == "aligned":
        macro_alignment = "aligned"
    return grid_opportunity_from_plan(plan, macro_alignment=macro_alignment)


def build_live_strategy_selection(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    timeframe: str,
    capital_usd: Decimal,
    min_signal_score: int,
    base_macro_alignment: str,
    minimum_score: Decimal = Decimal("0.55"),
    conflict_margin: Decimal = Decimal("0.08"),
    overlay_signal: DogeSignalSnapshot | None = None,
    feedback_policy: SelectorFeedbackPolicy | None = None,
) -> StrategySelection:
    opportunities = (
        _build_overlay_opportunity(
            executor,
            symbol=symbol,
            timeframe=timeframe,
            notional_usd=capital_usd,
            min_signal_score=min_signal_score,
            macro_alignment=base_macro_alignment,
            overlay_signal=overlay_signal,
        ),
        _build_arbitrage_opportunity(
            executor,
            symbol=symbol,
            capital_usd=capital_usd,
            base_macro_alignment=base_macro_alignment,
        ),
        _build_grid_opportunity(
            executor,
            symbol=symbol,
            capital_usd=capital_usd,
            base_macro_alignment=base_macro_alignment,
        ),
    )
    selection = select_doge_strategy(
        opportunities,
        minimum_score=minimum_score,
        conflict_margin=conflict_margin,
    )
    resolved_feedback_policy = feedback_policy or _default_feedback_policy()
    if resolved_feedback_policy.resolved_mode == "off":
        return selection

    scorecard = get_doge_strategy_scorecard(
        days=max(1, int(resolved_feedback_policy.window_days)),
        end_date=datetime.now(timezone.utc).date().isoformat(),
    )
    return attach_selector_feedback(
        selection,
        scorecard_summary=scorecard,
        policy=resolved_feedback_policy,
        minimum_score=minimum_score,
        conflict_margin=conflict_margin,
    )