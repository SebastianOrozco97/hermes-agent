from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Mapping, Sequence

from tools.doge_arbitrage_advisor import ArbitragePlan
from tools.doge_grid_advisor import GridPlan
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
class StrategySelection:
    symbol: str
    chosen_opportunity: StrategyOpportunity
    ranked_opportunities: tuple[RankedOpportunity, ...]
    abstained: bool
    abstain_reason: str = ""

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
        regime_tags=_normalize_text_items(regime_tags),
        operator_summary=str(operator_summary or "").strip() or "No DOGE strategy currently deserves allocation.",
        diagnostic_payload={"blocker_count": len(normalized_blockers)},
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

    regime_tags = ["directional_overlay", str(signal.timeframe or "15m").strip() or "15m"]
    if signal.last_close >= signal.breakout_reference:
        regime_tags.append("breakout_pressure")
    if signal.volume_ratio >= Decimal("1.10"):
        regime_tags.append("volume_confirmed")
    if signal.ema_fast > signal.ema_slow:
        regime_tags.append("trend_supportive")

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
        regime_tags=_normalize_text_items(regime_tags),
        operator_summary=operator_summary,
        diagnostic_payload={
            "signal": signal.to_dict(),
            "score_threshold_family": "directional_breakout",
        },
    )


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
        regime_tags=("funding_carry", "delta_neutral"),
        operator_summary=operator_summary,
        diagnostic_payload={
            "action": plan.action,
            "spot_notional_usd": _decimal_text(plan.spot_notional_usd),
            "futures_notional_usd": _decimal_text(plan.futures_notional_usd),
            "futures_margin_usd": _decimal_text(plan.futures_margin_usd),
            "delta_gap_pct": _decimal_text(plan.delta_gap_pct),
            "expected_yield_pct": _decimal_text(plan.expected_yield_pct),
            "rationale": plan.rationale,
        },
    )


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
        regime_tags=_normalize_text_items(("range_capture", plan.regime)),
        operator_summary=operator_summary,
        diagnostic_payload={
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "level_count": len(plan.levels),
            "stop_loss_price_lower": _decimal_text(plan.stop_loss_price_lower),
            "stop_loss_price_upper": _decimal_text(plan.stop_loss_price_upper),
            "rationale": plan.rationale,
        },
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

    ordered = sorted(
        provisional,
        key=lambda item: (
            not item.eligible_for_selection,
            -item.selection_score,
            -item.opportunity.expected_edge,
            -item.opportunity.confidence,
            item.opportunity.capital_required_usd,
            item.opportunity.strategy_id,
        ),
    )

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
            opportunities=normalized_opportunities,
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
            opportunities=normalized_opportunities,
            ranked_opportunities=ranked_opportunities,
            blockers=(abstain_reason,),
            operator_summary="DOGE selector abstained because the evidence is too weak.",
            abstain_reason=abstain_reason,
        )

    if len(eligible_ranked) > 1:
        second_ranked = eligible_ranked[1]
        score_gap = top_ranked.selection_score - second_ranked.selection_score
        if score_gap < conflict_margin:
            abstain_reason = (
                f"conflicting opportunities {top_ranked.opportunity.strategy_id} and "
                f"{second_ranked.opportunity.strategy_id} are separated by only {_decimal_text(score_gap)}"
            )
            return _build_abstention(
                symbol=symbol,
                opportunities=normalized_opportunities,
                ranked_opportunities=ranked_opportunities,
                blockers=(abstain_reason,),
                operator_summary="DOGE selector abstained because the leading strategies are too close to call.",
                abstain_reason=abstain_reason,
            )

    return StrategySelection(
        symbol=symbol,
        chosen_opportunity=top_ranked.opportunity,
        ranked_opportunities=ranked_opportunities,
        abstained=False,
        abstain_reason="",
    )