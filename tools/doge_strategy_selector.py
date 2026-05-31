from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from tools.doge_arbitrage_advisor import ArbitragePlan
from tools.doge_grid_advisor import GridPlan
from tools.doge_regime_classifier import (
    classify_arbitrage_regime,
    classify_grid_regime,
    classify_no_trade_regime,
    classify_overlay_regime,
)
from tools.doge_signal_engine import DogeSignalSnapshot


def _clamp_decimal(value: Decimal, *, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


def _decimal_text(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


def _normalize_text_items(values: Sequence[str]) -> tuple[str, ...]:
    items: list[str] = []
    for raw_value in values:
        value = str(raw_value or "").strip()
        if value and value not in items:
            items.append(value)
    return tuple(items)


def _normalize_macro_alignment(value: str) -> str:
    return str(value or "aligned").strip().lower() or "aligned"


_STRATEGY_SELECTION_PRIORITY = {
    "funding_arbitrage": 0,
    "atr_grid": 1,
    "overlay_tactical_long": 2,
    "no_trade": 99,
}


@dataclass(frozen=True)
class StrategyOpportunity:
    strategy_id: str
    symbol: str
    action: str
    eligible: bool
    blockers: tuple[str, ...]
    expected_edge: Decimal
    confidence: Decimal
    capital_required_usd: Decimal
    holding_horizon: str
    macro_alignment: str
    regime_tags: tuple[str, ...]
    operator_summary: str
    diagnostic_payload: Mapping[str, Any] = field(default_factory=dict)
    primary_regime: str = "unknown"

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "symbol": self.symbol,
            "action": self.action,
            "eligible": self.eligible,
            "blockers": list(self.blockers),
            "expected_edge": _decimal_text(self.expected_edge),
            "confidence": _decimal_text(self.confidence),
            "capital_required_usd": _decimal_text(self.capital_required_usd),
            "holding_horizon": self.holding_horizon,
            "macro_alignment": self.macro_alignment,
            "primary_regime": self.primary_regime,
            "regime_tags": list(self.regime_tags),
            "operator_summary": self.operator_summary,
            "diagnostic_payload": dict(self.diagnostic_payload),
        }


@dataclass(frozen=True)
class RankedOpportunity:
    opportunity: StrategyOpportunity
    rank: int
    selection_score: Decimal
    eligible_for_selection: bool
    rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "selection_score": _decimal_text(self.selection_score),
            "eligible_for_selection": self.eligible_for_selection,
            "rejection_reason": self.rejection_reason,
            "opportunity": self.opportunity.to_dict(),
        }


@dataclass(frozen=True)
class SelectorFeedbackPolicy:
    mode: str = "shadow"
    window_days: int = 14
    minimum_closed_positions: int = 5
    minimum_approvals_requested: int = 5
    diagnostic_only_closed_positions: int = 8
    diagnostic_only_approvals_requested: int = 8
    positive_expectancy_usd: Decimal = Decimal("0.10")
    negative_expectancy_usd: Decimal = Decimal("-0.05")
    positive_conversion_pct: Decimal = Decimal("60")
    negative_conversion_pct: Decimal = Decimal("35")
    score_bonus: Decimal = Decimal("0.05")
    score_penalty: Decimal = Decimal("0.08")

    @property
    def resolved_mode(self) -> str:
        normalized = str(self.mode or "shadow").strip().lower() or "shadow"
        if normalized not in {"off", "shadow", "active"}:
            return "shadow"
        return normalized

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.resolved_mode,
            "window_days": self.window_days,
            "minimum_closed_positions": self.minimum_closed_positions,
            "minimum_approvals_requested": self.minimum_approvals_requested,
            "diagnostic_only_closed_positions": self.diagnostic_only_closed_positions,
            "diagnostic_only_approvals_requested": self.diagnostic_only_approvals_requested,
            "positive_expectancy_usd": _decimal_text(self.positive_expectancy_usd),
            "negative_expectancy_usd": _decimal_text(self.negative_expectancy_usd),
            "positive_conversion_pct": _decimal_text(self.positive_conversion_pct),
            "negative_conversion_pct": _decimal_text(self.negative_conversion_pct),
            "score_bonus": _decimal_text(self.score_bonus),
            "score_penalty": _decimal_text(self.score_penalty),
        }


@dataclass(frozen=True)
class StrategyFeedbackEvaluation:
    strategy_id: str
    primary_regime: str
    policy_action: str
    policy_reason: str
    sample_count: int
    approvals_requested: int
    approval_conversion_pct: Decimal
    expectancy_usd: Decimal
    hit_rate_pct: Decimal
    realized_pnl_usd: Decimal
    base_rank: int
    shadow_rank: int
    base_selection_score: Decimal
    shadow_selection_score: Decimal
    eligible_for_shadow: bool
    shadow_rejection_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "primary_regime": self.primary_regime,
            "policy_action": self.policy_action,
            "policy_reason": self.policy_reason,
            "sample_count": self.sample_count,
            "approvals_requested": self.approvals_requested,
            "approval_conversion_pct": _decimal_text(self.approval_conversion_pct),
            "expectancy_usd": _decimal_text(self.expectancy_usd),
            "hit_rate_pct": _decimal_text(self.hit_rate_pct),
            "realized_pnl_usd": _decimal_text(self.realized_pnl_usd),
            "base_rank": self.base_rank,
            "shadow_rank": self.shadow_rank,
            "base_selection_score": _decimal_text(self.base_selection_score),
            "shadow_selection_score": _decimal_text(self.shadow_selection_score),
            "eligible_for_shadow": self.eligible_for_shadow,
            "shadow_rejection_reason": self.shadow_rejection_reason,
        }


@dataclass(frozen=True)
class SelectorFeedbackResult:
    policy: SelectorFeedbackPolicy
    scorecard_start_date: str
    scorecard_end_date: str
    evaluations: tuple[StrategyFeedbackEvaluation, ...]
    shadow_chosen_strategy_id: str
    shadow_abstained: bool
    shadow_abstain_reason: str = ""
    shadow_would_change_selection: bool = False
    shadow_would_change_abstention: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy": self.policy.to_dict(),
            "scorecard_window": {
                "start_date": self.scorecard_start_date,
                "end_date": self.scorecard_end_date,
            },
            "evaluations": [evaluation.to_dict() for evaluation in self.evaluations],
            "shadow_selection": {
                "chosen_strategy_id": self.shadow_chosen_strategy_id,
                "abstained": self.shadow_abstained,
                "abstain_reason": self.shadow_abstain_reason,
                "would_change_selection": self.shadow_would_change_selection,
                "would_change_abstention": self.shadow_would_change_abstention,
            },
        }


@dataclass(frozen=True)
class StrategySelection:
    symbol: str
    chosen_opportunity: StrategyOpportunity
    ranked_opportunities: tuple[RankedOpportunity, ...]
    abstained: bool
    abstain_reason: str = ""
    feedback_result: SelectorFeedbackResult | None = None

    @property
    def chosen_strategy_id(self) -> str:
        return self.chosen_opportunity.strategy_id

    @property
    def rejected_alternatives(self) -> tuple[RankedOpportunity, ...]:
        return tuple(
            ranked
            for ranked in self.ranked_opportunities
            if ranked.opportunity.strategy_id != self.chosen_opportunity.strategy_id
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "chosen_strategy_id": self.chosen_strategy_id,
            "chosen_opportunity": self.chosen_opportunity.to_dict(),
            "ranked_opportunities": [ranked.to_dict() for ranked in self.ranked_opportunities],
            "abstained": self.abstained,
            "abstain_reason": self.abstain_reason,
            "feedback_result": self.feedback_result.to_dict() if self.feedback_result is not None else None,
        }


def build_no_trade_opportunity(
    *,
    symbol: str,
    blockers: Sequence[str],
    macro_alignment: str = "aligned",
    regime_tags: Sequence[str] = (),
    operator_summary: str = "No DOGE strategy currently deserves allocation.",
) -> StrategyOpportunity:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")

    normalized_blockers = _normalize_text_items(blockers)
    if not normalized_blockers:
        raise ValueError("at least one blocker is required for a no-trade opportunity")

    classification = classify_no_trade_regime(
        blockers=normalized_blockers,
        macro_alignment=_normalize_macro_alignment(macro_alignment),
        regime_tags=_normalize_text_items(regime_tags),
    )

    return StrategyOpportunity(
        strategy_id="no_trade",
        symbol=normalized_symbol,
        action="hold_cash",
        eligible=False,
        blockers=normalized_blockers,
        expected_edge=Decimal("0"),
        confidence=Decimal("1"),
        capital_required_usd=Decimal("0"),
        holding_horizon="wait",
        macro_alignment=_normalize_macro_alignment(macro_alignment),
        regime_tags=classification.regime_tags,
        operator_summary=str(operator_summary or "").strip() or "No DOGE strategy currently deserves allocation.",
        diagnostic_payload={"blocker_count": len(normalized_blockers)},
        primary_regime=classification.primary_regime,
    )


def overlay_opportunity_from_signal(
    signal: DogeSignalSnapshot,
    *,
    notional_usd: Decimal,
    macro_alignment: str = "aligned",
) -> StrategyOpportunity:
    capital_required_usd = Decimal(str(notional_usd).strip())
    if capital_required_usd < 0:
        raise ValueError("notional_usd must be zero or positive")

    verdict = str(signal.verdict or "").strip().lower()
    eligible = verdict == "candidate_long"
    blockers: list[str] = []
    if not eligible:
        blockers.append(f"signal verdict is {verdict or 'unknown'}")
    if capital_required_usd <= 0:
        blockers.append("notional_usd must be greater than zero")
        eligible = False

    classification = classify_overlay_regime(
        signal,
        macro_alignment=macro_alignment,
    )

    operator_summary = (
        f"Overlay {signal.timeframe}: verdict {signal.verdict}, score {signal.signal_score}/7, "
        f"confidence {_decimal_text(signal.verifier_confidence)}."
    )

    return StrategyOpportunity(
        strategy_id="overlay_tactical_long",
        symbol=str(signal.symbol or "").strip().upper(),
        action="enter_long" if eligible else "hold",
        eligible=eligible,
        blockers=tuple(blockers),
        expected_edge=_clamp_decimal(
            Decimal(signal.signal_score) / Decimal("7"),
            low=Decimal("0"),
            high=Decimal("1"),
        ),
        confidence=_clamp_decimal(
            Decimal(signal.verifier_confidence),
            low=Decimal("0"),
            high=Decimal("1"),
        ),
        capital_required_usd=capital_required_usd,
        holding_horizon="30-90m",
        macro_alignment=_normalize_macro_alignment(macro_alignment),
        regime_tags=classification.regime_tags,
        operator_summary=operator_summary,
        diagnostic_payload={
            "signal": signal.to_dict(),
            "score_threshold_family": "directional_breakout",
        },
        primary_regime=classification.primary_regime,
    )


def _serialize_arbitrage_plan(plan: ArbitragePlan) -> dict[str, Any]:
    return {
        "action": str(plan.action or "").strip(),
        "symbol": str(plan.symbol or "").strip().upper(),
        "spot_quantity": _decimal_text(plan.spot_quantity),
        "futures_quantity": _decimal_text(plan.futures_quantity),
        "leverage": _decimal_text(plan.leverage),
        "spot_notional_usd": _decimal_text(plan.spot_notional_usd),
        "futures_notional_usd": _decimal_text(plan.futures_notional_usd),
        "futures_margin_usd": _decimal_text(plan.futures_margin_usd),
        "delta_gap_pct": _decimal_text(plan.delta_gap_pct),
        "expected_yield_pct": _decimal_text(plan.expected_yield_pct),
        "rationale": str(plan.rationale or "").strip(),
    }


def arbitrage_opportunity_from_plan(
    plan: ArbitragePlan,
    *,
    macro_alignment: str = "aligned",
) -> StrategyOpportunity:
    eligible = str(plan.action or "").strip().lower() == "enter_arbitrage"
    blockers: list[str] = []
    if not eligible:
        blockers.append("funding is below the arbitrage entry threshold")

    expected_edge = _clamp_decimal(
        plan.expected_yield_pct / Decimal("0.30"),
        low=Decimal("0"),
        high=Decimal("1"),
    )
    confidence = _clamp_decimal(
        Decimal("0.40")
        + (expected_edge * Decimal("0.45"))
        - _clamp_decimal(plan.delta_gap_pct / Decimal("2"), low=Decimal("0"), high=Decimal("1")) * Decimal("0.15"),
        low=Decimal("0.05"),
        high=Decimal("0.95"),
    )

    operator_summary = (
        f"Funding arbitrage: action {plan.action}, expected funding yield {_decimal_text(plan.expected_yield_pct)}%, "
        f"delta gap {_decimal_text(plan.delta_gap_pct)}%."
    )

    classification = classify_arbitrage_regime(
        plan,
        macro_alignment=macro_alignment,
    )

    return StrategyOpportunity(
        strategy_id="funding_arbitrage",
        symbol=str(plan.symbol or "").strip().upper(),
        action="enter_arbitrage" if eligible else "hold",
        eligible=eligible,
        blockers=tuple(blockers),
        expected_edge=expected_edge,
        confidence=confidence,
        capital_required_usd=Decimal(plan.spot_notional_usd) + Decimal(plan.futures_margin_usd),
        holding_horizon="4-12h",
        macro_alignment=_normalize_macro_alignment(macro_alignment),
        regime_tags=classification.regime_tags,
        operator_summary=operator_summary,
        diagnostic_payload={
            "action": plan.action,
            "spot_notional_usd": _decimal_text(plan.spot_notional_usd),
            "futures_notional_usd": _decimal_text(plan.futures_notional_usd),
            "futures_margin_usd": _decimal_text(plan.futures_margin_usd),
            "delta_gap_pct": _decimal_text(plan.delta_gap_pct),
            "expected_yield_pct": _decimal_text(plan.expected_yield_pct),
            "rationale": plan.rationale,
            "plan": _serialize_arbitrage_plan(plan),
        },
        primary_regime=classification.primary_regime,
    )


def _serialize_grid_plan(plan: GridPlan) -> dict[str, Any]:
    return {
        "symbol": str(plan.symbol or "").strip().upper(),
        "market_price": _decimal_text(plan.market_price),
        "levels": [
            {
                "price": _decimal_text(level.price),
                "side": str(level.side or "").strip().upper(),
                "quantity": _decimal_text(level.quantity),
            }
            for level in plan.levels
        ],
        "total_required_capital": _decimal_text(plan.total_required_capital),
        "stop_loss_price_lower": _decimal_text(plan.stop_loss_price_lower),
        "stop_loss_price_upper": _decimal_text(plan.stop_loss_price_upper),
        "leverage": _decimal_text(plan.leverage),
        "regime": str(plan.regime or "").strip(),
        "regime_reason": str(plan.regime_reason or "").strip(),
        "regime_allows_entry": bool(plan.regime_allows_entry),
        "rationale": str(plan.rationale or "").strip(),
    }


def grid_opportunity_from_plan(
    plan: GridPlan,
    *,
    macro_alignment: str = "aligned",
) -> StrategyOpportunity:
    eligible = bool(plan.regime_allows_entry)
    blockers: list[str] = []
    if not eligible:
        blockers.append(plan.regime_reason)

    edge_by_regime = {
        "range_bound": Decimal("0.60"),
        "trend": Decimal("0.18"),
        "high_volatility": Decimal("0.12"),
    }
    confidence_by_regime = {
        "range_bound": Decimal("0.70"),
        "trend": Decimal("0.25"),
        "high_volatility": Decimal("0.20"),
    }
    regime = str(plan.regime or "unknown").strip().lower() or "unknown"
    expected_edge = edge_by_regime.get(regime, Decimal("0.10"))
    confidence = confidence_by_regime.get(regime, Decimal("0.20"))

    operator_summary = (
        f"Grid {plan.symbol}: regime {plan.regime}, levels {len(plan.levels)}, "
        f"capital {_decimal_text(plan.total_required_capital)}."
    )

    classification = classify_grid_regime(
        plan,
        macro_alignment=macro_alignment,
    )

    return StrategyOpportunity(
        strategy_id="atr_grid",
        symbol=str(plan.symbol or "").strip().upper(),
        action="seed_grid" if eligible else "hold",
        eligible=eligible,
        blockers=tuple(blockers),
        expected_edge=expected_edge,
        confidence=confidence,
        capital_required_usd=Decimal(plan.total_required_capital),
        holding_horizon="1-3d until breakout",
        macro_alignment=_normalize_macro_alignment(macro_alignment),
        regime_tags=classification.regime_tags,
        operator_summary=operator_summary,
        diagnostic_payload={
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "level_count": len(plan.levels),
            "stop_loss_price_lower": _decimal_text(plan.stop_loss_price_lower),
            "stop_loss_price_upper": _decimal_text(plan.stop_loss_price_upper),
            "rationale": plan.rationale,
            "plan": _serialize_grid_plan(plan),
        },
        primary_regime=classification.primary_regime,
    )


def _combined_regime_tags(opportunities: Sequence[StrategyOpportunity]) -> tuple[str, ...]:
    tags: list[str] = []
    for opportunity in opportunities:
        tags.extend(opportunity.regime_tags)
    return _normalize_text_items(tags)


def _combined_macro_alignment(opportunities: Sequence[StrategyOpportunity]) -> str:
    states = {_normalize_macro_alignment(opportunity.macro_alignment) for opportunity in opportunities}
    for candidate in ("blocked", "divergent", "cautious", "aligned"):
        if candidate in states:
            return candidate
    return "aligned"


def _diagnostic_flag(payload: Mapping[str, Any], key: str) -> bool | None:
    value = payload.get(key)
    if value is None:
        return None
    return bool(value)


def _has_forcing_blocker(opportunity: StrategyOpportunity) -> str:
    macro_alignment = _normalize_macro_alignment(opportunity.macro_alignment)
    if macro_alignment in {"blocked", "divergent"}:
        return f"macro alignment is {macro_alignment}"

    blocker_keywords = {
        "execution": (
            "execution",
            "exchange",
            "balance",
            "margin",
            "approval",
            "reserve",
            "guardrail",
            "liquidity",
        ),
        "sample": (
            "sample",
            "history",
            "calibration",
            "insufficient data",
            "insufficient evidence",
        ),
        "macro": (
            "macro",
            "btc",
        ),
    }
    for blocker in opportunity.blockers:
        lowered = blocker.lower()
        for keywords in blocker_keywords.values():
            if any(keyword in lowered for keyword in keywords):
                return blocker

    sample_size_ready = _diagnostic_flag(opportunity.diagnostic_payload, "sample_size_ready")
    if sample_size_ready is False:
        return "sample size gate is not ready"

    execution_ready = _diagnostic_flag(opportunity.diagnostic_payload, "execution_ready")
    if execution_ready is False:
        return "execution path is not ready"

    if opportunity.expected_edge <= 0:
        return "expected edge is not positive"
    if opportunity.confidence <= 0:
        return "confidence is not positive"

    return ""


def _selection_score(opportunity: StrategyOpportunity) -> Decimal:
    score = (opportunity.expected_edge * Decimal("0.60")) + (opportunity.confidence * Decimal("0.40"))
    macro_alignment = _normalize_macro_alignment(opportunity.macro_alignment)
    if macro_alignment == "cautious":
        score -= Decimal("0.08")
    return _clamp_decimal(score, low=Decimal("0"), high=Decimal("1"))


def _strategy_selection_priority(opportunity: StrategyOpportunity) -> int:
    normalized = str(opportunity.strategy_id or "").strip().lower()
    return _STRATEGY_SELECTION_PRIORITY.get(normalized, 50)


def _rank_sort_key(item: RankedOpportunity) -> tuple[bool, Decimal, Decimal, Decimal, Decimal, str]:
    return (
        not item.eligible_for_selection,
        _strategy_selection_priority(item.opportunity),
        -item.selection_score,
        -item.opportunity.expected_edge,
        -item.opportunity.confidence,
        item.opportunity.capital_required_usd,
        item.opportunity.strategy_id,
    )


def _ranked_opportunities(opportunities: Sequence[StrategyOpportunity]) -> tuple[RankedOpportunity, ...]:
    provisional: list[RankedOpportunity] = []
    for opportunity in opportunities:
        rejection_reason = ""
        eligible_for_selection = bool(opportunity.eligible)
        if not eligible_for_selection:
            rejection_reason = "; ".join(opportunity.blockers) or "strategy is not eligible"
        else:
            rejection_reason = _has_forcing_blocker(opportunity)
            eligible_for_selection = not rejection_reason

        selection_score = _selection_score(opportunity) if eligible_for_selection else Decimal("0")
        provisional.append(
            RankedOpportunity(
                opportunity=opportunity,
                rank=0,
                selection_score=selection_score,
                eligible_for_selection=eligible_for_selection,
                rejection_reason=rejection_reason,
            )
        )

    ordered = sorted(provisional, key=_rank_sort_key)

    ranked: list[RankedOpportunity] = []
    for index, item in enumerate(ordered, start=1):
        ranked.append(
            RankedOpportunity(
                opportunity=item.opportunity,
                rank=index,
                selection_score=item.selection_score,
                eligible_for_selection=item.eligible_for_selection,
                rejection_reason=item.rejection_reason,
            )
        )
    return tuple(ranked)


def _build_abstention(
    *,
    symbol: str,
    opportunities: Sequence[StrategyOpportunity],
    ranked_opportunities: Sequence[RankedOpportunity],
    blockers: Sequence[str],
    operator_summary: str,
    abstain_reason: str,
) -> StrategySelection:
    chosen_opportunity = build_no_trade_opportunity(
        symbol=symbol,
        blockers=blockers,
        macro_alignment=_combined_macro_alignment(opportunities),
        regime_tags=_combined_regime_tags(opportunities),
        operator_summary=operator_summary,
    )
    return StrategySelection(
        symbol=symbol,
        chosen_opportunity=chosen_opportunity,
        ranked_opportunities=tuple(ranked_opportunities),
        abstained=True,
        abstain_reason=abstain_reason,
    )


def _selection_from_ranked(
    *,
    symbol: str,
    opportunities: Sequence[StrategyOpportunity],
    ranked_opportunities: Sequence[RankedOpportunity],
    minimum_score: Decimal,
    conflict_margin: Decimal,
) -> StrategySelection:
    eligible_ranked = tuple(ranked for ranked in ranked_opportunities if ranked.eligible_for_selection)

    if not eligible_ranked:
        rejection_blockers = _normalize_text_items(
            [
                f"{ranked.opportunity.strategy_id}: {ranked.rejection_reason or 'strategy is not eligible'}"
                for ranked in ranked_opportunities
            ]
        )
        abstain_reason = "every strategy lane is blocked or incomplete"
        return _build_abstention(
            symbol=symbol,
            opportunities=opportunities,
            ranked_opportunities=ranked_opportunities,
            blockers=rejection_blockers,
            operator_summary="DOGE selector abstained because every strategy lane is blocked.",
            abstain_reason=abstain_reason,
        )

    top_ranked = eligible_ranked[0]
    if top_ranked.selection_score < minimum_score:
        abstain_reason = (
            f"top opportunity score {_decimal_text(top_ranked.selection_score)} is below minimum "
            f"{_decimal_text(minimum_score)}"
        )
        return _build_abstention(
            symbol=symbol,
            opportunities=opportunities,
            ranked_opportunities=ranked_opportunities,
            blockers=(abstain_reason,),
            operator_summary="DOGE selector abstained because the evidence is too weak.",
            abstain_reason=abstain_reason,
        )

    if len(eligible_ranked) > 1:
        second_ranked = eligible_ranked[1]
        same_priority = _strategy_selection_priority(top_ranked.opportunity) == _strategy_selection_priority(second_ranked.opportunity)
        if same_priority:
            score_gap = top_ranked.selection_score - second_ranked.selection_score
            if score_gap < conflict_margin:
                abstain_reason = (
                    f"conflicting opportunities {top_ranked.opportunity.strategy_id} and "
                    f"{second_ranked.opportunity.strategy_id} are separated by only {_decimal_text(score_gap)}"
                )
                return _build_abstention(
                    symbol=symbol,
                    opportunities=opportunities,
                    ranked_opportunities=ranked_opportunities,
                    blockers=(abstain_reason,),
                    operator_summary="DOGE selector abstained because the leading strategies are too close to call.",
                    abstain_reason=abstain_reason,
                )

    return StrategySelection(
        symbol=symbol,
        chosen_opportunity=top_ranked.opportunity,
        ranked_opportunities=tuple(ranked_opportunities),
        abstained=False,
        abstain_reason="",
    )


def _scorecard_decimal(value: Any, default: str = "0") -> Decimal:
    text = str(value if value not in (None, "") else default).strip() or default
    try:
        return Decimal(text)
    except Exception:
        return Decimal(default)


def _scorecard_pair(
    summary: Mapping[str, Any],
    *,
    strategy_id: str,
    primary_regime: str,
) -> Mapping[str, Any] | None:
    for item in list(summary.get("strategy_regime_pairs") or []):
        if not isinstance(item, Mapping):
            continue
        if str(item.get("strategy_id", "") or "").strip().lower() != strategy_id:
            continue
        if str(item.get("regime_label", "") or "").strip().lower() != primary_regime:
            continue
        return item
    return None


def attach_selector_feedback(
    selection: StrategySelection,
    *,
    scorecard_summary: Mapping[str, Any],
    policy: SelectorFeedbackPolicy | None = None,
    minimum_score: Decimal = Decimal("0.55"),
    conflict_margin: Decimal = Decimal("0.08"),
) -> StrategySelection:
    resolved_policy = policy or SelectorFeedbackPolicy()
    if resolved_policy.resolved_mode == "off":
        return selection

    opportunities = tuple(ranked.opportunity for ranked in selection.ranked_opportunities)
    provisional_shadow: list[RankedOpportunity] = []
    provisional_evaluations: list[dict[str, Any]] = []

    for ranked in selection.ranked_opportunities:
        opportunity = ranked.opportunity
        primary_regime = str(opportunity.primary_regime or "unknown").strip().lower() or "unknown"
        metrics = _scorecard_pair(
            scorecard_summary,
            strategy_id=str(opportunity.strategy_id or "").strip().lower(),
            primary_regime=primary_regime,
        )
        sample_count = int((metrics or {}).get("sample_count", 0) or 0)
        approvals_requested = int((metrics or {}).get("approvals_requested", 0) or 0)
        approval_conversion_pct = _scorecard_decimal((metrics or {}).get("approval_conversion_pct", "0"))
        expectancy_usd = _scorecard_decimal((metrics or {}).get("expectancy_usd", "0"))
        hit_rate_pct = _scorecard_decimal((metrics or {}).get("hit_rate_pct", (metrics or {}).get("win_rate_pct", "0")))
        realized_pnl_usd = _scorecard_decimal((metrics or {}).get("realized_pnl_usd", "0"))

        policy_action = "neutral"
        policy_reason = "feedback policy sees no material reason to change this lane"
        shadow_score = ranked.selection_score
        eligible_for_shadow = ranked.eligible_for_selection
        shadow_rejection_reason = ranked.rejection_reason

        if not ranked.eligible_for_selection:
            policy_action = "blocked"
            policy_reason = ranked.rejection_reason or "strategy is already blocked before feedback policy"
        elif metrics is None:
            policy_action = "insufficient_sample"
            policy_reason = "no strategy x regime scorecard exists yet for this lane"
        elif (
            sample_count < resolved_policy.minimum_closed_positions
            or approvals_requested < resolved_policy.minimum_approvals_requested
        ):
            policy_action = "insufficient_sample"
            policy_reason = (
                f"sample {sample_count}/{resolved_policy.minimum_closed_positions} or approvals "
                f"{approvals_requested}/{resolved_policy.minimum_approvals_requested} are below policy minimum"
            )
        elif (
            sample_count >= resolved_policy.diagnostic_only_closed_positions
            and approvals_requested >= resolved_policy.diagnostic_only_approvals_requested
            and expectancy_usd <= resolved_policy.negative_expectancy_usd
            and approval_conversion_pct <= resolved_policy.negative_conversion_pct
        ):
            policy_action = "diagnostic_only"
            policy_reason = (
                f"persistent negative evidence: expectancy {_decimal_text(expectancy_usd)} USD and conversion "
                f"{_decimal_text(approval_conversion_pct)}%"
            )
            eligible_for_shadow = False
            shadow_score = Decimal("0")
            shadow_rejection_reason = policy_reason
        elif (
            expectancy_usd <= resolved_policy.negative_expectancy_usd
            or approval_conversion_pct <= resolved_policy.negative_conversion_pct
        ):
            policy_action = "penalize"
            policy_reason = (
                f"weak evidence: expectancy {_decimal_text(expectancy_usd)} USD and conversion "
                f"{_decimal_text(approval_conversion_pct)}%"
            )
            shadow_score = _clamp_decimal(
                ranked.selection_score - resolved_policy.score_penalty,
                low=Decimal("0"),
                high=Decimal("1"),
            )
        elif (
            expectancy_usd >= resolved_policy.positive_expectancy_usd
            and approval_conversion_pct >= resolved_policy.positive_conversion_pct
        ):
            policy_action = "boost"
            policy_reason = (
                f"strong evidence: expectancy {_decimal_text(expectancy_usd)} USD and conversion "
                f"{_decimal_text(approval_conversion_pct)}%"
            )
            shadow_score = _clamp_decimal(
                ranked.selection_score + resolved_policy.score_bonus,
                low=Decimal("0"),
                high=Decimal("1"),
            )

        provisional_shadow.append(
            RankedOpportunity(
                opportunity=opportunity,
                rank=0,
                selection_score=shadow_score if eligible_for_shadow else Decimal("0"),
                eligible_for_selection=eligible_for_shadow,
                rejection_reason=shadow_rejection_reason,
            )
        )
        provisional_evaluations.append(
            {
                "strategy_id": opportunity.strategy_id,
                "primary_regime": primary_regime,
                "policy_action": policy_action,
                "policy_reason": policy_reason,
                "sample_count": sample_count,
                "approvals_requested": approvals_requested,
                "approval_conversion_pct": approval_conversion_pct,
                "expectancy_usd": expectancy_usd,
                "hit_rate_pct": hit_rate_pct,
                "realized_pnl_usd": realized_pnl_usd,
                "base_rank": ranked.rank,
                "base_selection_score": ranked.selection_score,
                "shadow_selection_score": shadow_score if eligible_for_shadow else Decimal("0"),
                "eligible_for_shadow": eligible_for_shadow,
                "shadow_rejection_reason": shadow_rejection_reason,
            }
        )

    ordered_shadow = sorted(provisional_shadow, key=_rank_sort_key)
    shadow_ranked: list[RankedOpportunity] = []
    shadow_rank_by_strategy: dict[str, int] = {}
    for index, item in enumerate(ordered_shadow, start=1):
        shadow_rank_by_strategy[item.opportunity.strategy_id] = index
        shadow_ranked.append(
            RankedOpportunity(
                opportunity=item.opportunity,
                rank=index,
                selection_score=item.selection_score,
                eligible_for_selection=item.eligible_for_selection,
                rejection_reason=item.rejection_reason,
            )
        )

    shadow_selection = _selection_from_ranked(
        symbol=selection.symbol,
        opportunities=opportunities,
        ranked_opportunities=tuple(shadow_ranked),
        minimum_score=minimum_score,
        conflict_margin=conflict_margin,
    )
    evaluations = tuple(
        StrategyFeedbackEvaluation(
            strategy_id=str(payload["strategy_id"]),
            primary_regime=str(payload["primary_regime"]),
            policy_action=str(payload["policy_action"]),
            policy_reason=str(payload["policy_reason"]),
            sample_count=int(payload["sample_count"]),
            approvals_requested=int(payload["approvals_requested"]),
            approval_conversion_pct=Decimal(payload["approval_conversion_pct"]),
            expectancy_usd=Decimal(payload["expectancy_usd"]),
            hit_rate_pct=Decimal(payload["hit_rate_pct"]),
            realized_pnl_usd=Decimal(payload["realized_pnl_usd"]),
            base_rank=int(payload["base_rank"]),
            shadow_rank=shadow_rank_by_strategy.get(str(payload["strategy_id"]), int(payload["base_rank"])),
            base_selection_score=Decimal(payload["base_selection_score"]),
            shadow_selection_score=Decimal(payload["shadow_selection_score"]),
            eligible_for_shadow=bool(payload["eligible_for_shadow"]),
            shadow_rejection_reason=str(payload["shadow_rejection_reason"] or ""),
        )
        for payload in provisional_evaluations
    )
    feedback_result = SelectorFeedbackResult(
        policy=resolved_policy,
        scorecard_start_date=str(scorecard_summary.get("start_date", "") or ""),
        scorecard_end_date=str(scorecard_summary.get("end_date", "") or ""),
        evaluations=tuple(sorted(evaluations, key=lambda item: item.shadow_rank)),
        shadow_chosen_strategy_id=shadow_selection.chosen_strategy_id,
        shadow_abstained=shadow_selection.abstained,
        shadow_abstain_reason=shadow_selection.abstain_reason,
        shadow_would_change_selection=shadow_selection.chosen_strategy_id != selection.chosen_strategy_id,
        shadow_would_change_abstention=shadow_selection.abstained != selection.abstained,
    )
    return StrategySelection(
        symbol=selection.symbol,
        chosen_opportunity=selection.chosen_opportunity,
        ranked_opportunities=selection.ranked_opportunities,
        abstained=selection.abstained,
        abstain_reason=selection.abstain_reason,
        feedback_result=feedback_result,
    )


def select_doge_strategy(
    opportunities: Sequence[StrategyOpportunity],
    *,
    minimum_score: Decimal = Decimal("0.55"),
    conflict_margin: Decimal = Decimal("0.08"),
) -> StrategySelection:
    if not opportunities:
        raise ValueError("at least one strategy opportunity is required")

    normalized_opportunities = tuple(opportunities)
    symbols = {str(opportunity.symbol or "").strip().upper() for opportunity in normalized_opportunities}
    symbols.discard("")
    if len(symbols) != 1:
        raise ValueError("all strategy opportunities must share the same symbol")
    symbol = next(iter(symbols))

    ranked_opportunities = _ranked_opportunities(normalized_opportunities)
    return _selection_from_ranked(
        symbol=symbol,
        opportunities=normalized_opportunities,
        ranked_opportunities=ranked_opportunities,
        minimum_score=minimum_score,
        conflict_margin=conflict_margin,
    )