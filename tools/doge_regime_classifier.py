from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from tools.doge_arbitrage_advisor import ArbitragePlan
from tools.doge_grid_advisor import GridPlan
from tools.doge_signal_engine import DogeSignalSnapshot
from tools.macro_data_oracle import classify_macro_alignment, classify_macro_regime


CANONICAL_DOGE_REGIMES = (
    "breakout_trend",
    "quiet_range",
    "high_volatility_stress",
    "funding_rich_carry",
    "macro_divergent_chop",
    "unknown",
)


@dataclass(frozen=True)
class DogeRegimeClassification:
    primary_regime: str
    regime_tags: tuple[str, ...]
    rationale: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "primary_regime": self.primary_regime,
            "regime_tags": list(self.regime_tags),
            "rationale": self.rationale,
        }


def _normalize_label(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text:
        return ""
    pieces: list[str] = []
    pending_separator = False
    for character in text:
        if character.isalnum():
            if pending_separator and pieces:
                pieces.append("_")
            pieces.append(character)
            pending_separator = False
        else:
            pending_separator = True
    return "".join(pieces).strip("_")


def _unique_labels(values: Sequence[Any]) -> tuple[str, ...]:
    labels: list[str] = []
    for raw_value in values:
        label = _normalize_label(raw_value)
        if label and label not in labels:
            labels.append(label)
    return tuple(labels)


def _parse_decimal(value: Any, default: str = "0") -> Decimal:
    text = str(value if value not in (None, "") else default).strip() or default
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal(default)


def _resolved_macro_alignment(macro_alignment: str, macro_state: Mapping[str, Any] | None) -> str:
    normalized = _normalize_label(macro_alignment)
    if normalized:
        return normalized
    if macro_state:
        return _normalize_label(classify_macro_alignment(macro_state)) or "aligned"
    return "aligned"


def _resolved_macro_regime(macro_state: Mapping[str, Any] | None) -> str:
    if not macro_state:
        return "balanced_macro"
    return _normalize_label(classify_macro_regime(macro_state)) or "macro_divergent_chop"


def _macro_regime_tags(macro_alignment: str, macro_state: Mapping[str, Any] | None) -> tuple[str, str]:
    macro_regime = _resolved_macro_regime(macro_state)
    macro_alignment_tag = f"macro_{macro_alignment}" if macro_alignment else "macro_aligned"
    return macro_regime, macro_alignment_tag


def _build_classification(
    primary_regime: str,
    *,
    rationale: str,
    base_tags: Sequence[Any],
    macro_alignment: str,
    macro_state: Mapping[str, Any] | None,
) -> DogeRegimeClassification:
    normalized_primary = _normalize_label(primary_regime) or "unknown"
    if normalized_primary not in CANONICAL_DOGE_REGIMES:
        normalized_primary = "unknown"
    macro_regime, macro_alignment_tag = _macro_regime_tags(macro_alignment, macro_state)
    regime_tags = _unique_labels(
        (
            normalized_primary,
            *base_tags,
            macro_alignment_tag,
            macro_regime,
        )
    )
    return DogeRegimeClassification(
        primary_regime=normalized_primary,
        regime_tags=regime_tags,
        rationale=str(rationale or "").strip() or normalized_primary,
    )


def classify_overlay_regime(
    signal: DogeSignalSnapshot,
    *,
    macro_alignment: str = "aligned",
    macro_state: Mapping[str, Any] | None = None,
) -> DogeRegimeClassification:
    resolved_macro_alignment = _resolved_macro_alignment(macro_alignment, macro_state)
    breakout_confirmed = signal.last_close > signal.breakout_reference
    volume_confirmed = signal.volume_ratio >= Decimal("1.10")
    trend_supportive = signal.ema_fast > signal.ema_slow
    macro_regime = _resolved_macro_regime(macro_state)

    if macro_regime == "high_volatility_stress" or resolved_macro_alignment == "blocked":
        primary_regime = "high_volatility_stress"
        rationale = "overlay setup sits inside a stressed macro backdrop"
    elif resolved_macro_alignment in {"divergent", "cautious"} or macro_regime == "macro_divergent_chop":
        primary_regime = "macro_divergent_chop"
        rationale = "overlay setup conflicts with macro direction or conviction"
    elif breakout_confirmed and volume_confirmed and trend_supportive:
        primary_regime = "breakout_trend"
        rationale = "breakout, volume, and trend all confirm directional continuation"
    elif signal.signal_score < 5 or signal.volume_ratio < Decimal("1.0"):
        primary_regime = "quiet_range"
        rationale = "directional evidence is soft and volume stays quiet"
    else:
        primary_regime = "macro_divergent_chop"
        rationale = "overlay setup lacks a clean directional or range regime confirmation"

    base_tags = ["directional_overlay", str(signal.timeframe or "15m").strip() or "15m"]
    if breakout_confirmed:
        base_tags.append("breakout_pressure")
    if volume_confirmed:
        base_tags.append("volume_confirmed")
    if trend_supportive:
        base_tags.append("trend_supportive")
    return _build_classification(
        primary_regime,
        rationale=rationale,
        base_tags=base_tags,
        macro_alignment=resolved_macro_alignment,
        macro_state=macro_state,
    )


def classify_arbitrage_regime(
    plan: ArbitragePlan,
    *,
    macro_alignment: str = "aligned",
    macro_state: Mapping[str, Any] | None = None,
) -> DogeRegimeClassification:
    resolved_macro_alignment = _resolved_macro_alignment(macro_alignment, macro_state)
    macro_regime = _resolved_macro_regime(macro_state)
    if macro_regime == "high_volatility_stress" and resolved_macro_alignment in {"blocked", "divergent"}:
        primary_regime = "high_volatility_stress"
        rationale = "carry conditions exist inside a stressed macro environment"
    elif (
        str(plan.action or "").strip().lower() == "enter_arbitrage"
        and plan.expected_yield_pct >= Decimal("0.10")
        and plan.delta_gap_pct <= Decimal("0.50")
        and resolved_macro_alignment != "blocked"
    ):
        primary_regime = "funding_rich_carry"
        rationale = "funding carry is rich and hedge imbalance stays contained"
    elif resolved_macro_alignment in {"blocked", "divergent", "cautious"} or macro_regime == "macro_divergent_chop":
        primary_regime = "macro_divergent_chop"
        rationale = "carry setup is weakened by macro divergence or caution"
    else:
        primary_regime = "quiet_range"
        rationale = "carry setup is weak and not rich enough to dominate the cycle"

    base_tags = ["funding_carry", "delta_neutral"]
    if plan.expected_yield_pct >= Decimal("0.10"):
        base_tags.append("carry_threshold_met")
    return _build_classification(
        primary_regime,
        rationale=rationale,
        base_tags=base_tags,
        macro_alignment=resolved_macro_alignment,
        macro_state=macro_state,
    )


def classify_grid_regime(
    plan: GridPlan,
    *,
    macro_alignment: str = "aligned",
    macro_state: Mapping[str, Any] | None = None,
) -> DogeRegimeClassification:
    resolved_macro_alignment = _resolved_macro_alignment(macro_alignment, macro_state)
    macro_regime = _resolved_macro_regime(macro_state)
    plan_regime = _normalize_label(plan.regime) or "unknown"

    if plan_regime == "high_volatility" or macro_regime == "high_volatility_stress":
        primary_regime = "high_volatility_stress"
        rationale = "grid range is invalid because volatility is too elevated"
    elif plan_regime == "range_bound" and resolved_macro_alignment == "aligned" and macro_regime != "macro_divergent_chop":
        primary_regime = "quiet_range"
        rationale = "grid conditions are range-bound with stable macro support"
    else:
        primary_regime = "macro_divergent_chop"
        rationale = "grid conditions conflict with trend or macro backdrop"

    return _build_classification(
        primary_regime,
        rationale=rationale,
        base_tags=("range_capture", plan_regime),
        macro_alignment=resolved_macro_alignment,
        macro_state=macro_state,
    )


def classify_no_trade_regime(
    *,
    blockers: Sequence[str],
    macro_alignment: str = "aligned",
    regime_tags: Sequence[str] = (),
    macro_state: Mapping[str, Any] | None = None,
) -> DogeRegimeClassification:
    normalized_tags = _unique_labels(regime_tags)
    blocker_text = " ".join(str(value or "").strip().lower() for value in blockers)
    for candidate in CANONICAL_DOGE_REGIMES:
        if candidate != "unknown" and candidate in normalized_tags:
            return _build_classification(
                candidate,
                rationale="no-trade path reuses the canonical regime carried by blockers or diagnostics",
                base_tags=normalized_tags,
                macro_alignment=_resolved_macro_alignment(macro_alignment, macro_state),
                macro_state=macro_state,
            )

    resolved_macro_alignment = _resolved_macro_alignment(macro_alignment, macro_state)
    macro_regime = _resolved_macro_regime(macro_state)
    if "high_volatility" in blocker_text or macro_regime == "high_volatility_stress" or resolved_macro_alignment == "blocked":
        primary_regime = "high_volatility_stress"
        rationale = "no-trade decision comes from stress or hard macro blockage"
    elif "range" in blocker_text or "quiet" in blocker_text:
        primary_regime = "quiet_range"
        rationale = "no-trade decision comes from a quiet regime with no dominant edge"
    elif "funding" in blocker_text or "carry" in blocker_text:
        primary_regime = "funding_rich_carry"
        rationale = "no-trade decision still sits inside a carry-driven regime"
    elif resolved_macro_alignment in {"divergent", "cautious"} or macro_regime == "macro_divergent_chop":
        primary_regime = "macro_divergent_chop"
        rationale = "no-trade decision comes from conflicting macro and local evidence"
    else:
        primary_regime = "unknown"
        rationale = "no-trade decision lacks a more specific canonical regime"

    return _build_classification(
        primary_regime,
        rationale=rationale,
        base_tags=normalized_tags,
        macro_alignment=resolved_macro_alignment,
        macro_state=macro_state,
    )


def classify_decision_context_regime(decision_context: Mapping[str, Any]) -> DogeRegimeClassification:
    payload = dict(decision_context or {})
    selected_strategy = payload.get("selected_strategy") or {}
    if not isinstance(selected_strategy, Mapping):
        selected_strategy = {}
    market_context = payload.get("market_context") or {}
    if not isinstance(market_context, Mapping):
        market_context = {}
    macro_state = payload.get("macro_state") or {}
    if not isinstance(macro_state, Mapping):
        macro_state = {}

    stored_primary = _normalize_label(selected_strategy.get("primary_regime"))
    strategy_id = _normalize_label(selected_strategy.get("strategy_id") or payload.get("selected_strategy_id") or "unknown")
    existing_tags = _unique_labels(selected_strategy.get("regime_tags") or market_context.get("regime_tags") or ())
    resolved_macro_alignment = _resolved_macro_alignment(selected_strategy.get("macro_alignment", ""), macro_state)

    if stored_primary in CANONICAL_DOGE_REGIMES:
        return _build_classification(
            stored_primary,
            rationale="decision context already carries a canonical regime label",
            base_tags=existing_tags,
            macro_alignment=resolved_macro_alignment,
            macro_state=macro_state,
        )

    if strategy_id == "overlay_tactical_long":
        signal_payload = market_context.get("signal") or {}
        breakout_confirmed = "breakout_pressure" in existing_tags or _parse_decimal(signal_payload.get("last_close")) > _parse_decimal(signal_payload.get("breakout_reference"))
        volume_confirmed = "volume_confirmed" in existing_tags or _parse_decimal(signal_payload.get("volume_ratio")) >= Decimal("1.10")
        trend_supportive = "trend_supportive" in existing_tags or _parse_decimal(signal_payload.get("ema_fast")) > _parse_decimal(signal_payload.get("ema_slow"))
        if not signal_payload and breakout_confirmed and trend_supportive and resolved_macro_alignment == "aligned":
            volume_confirmed = True
        if _resolved_macro_regime(macro_state) == "high_volatility_stress" or resolved_macro_alignment == "blocked":
            primary_regime = "high_volatility_stress"
            rationale = "historical overlay context resolves to macro stress"
        elif resolved_macro_alignment in {"divergent", "cautious"}:
            primary_regime = "macro_divergent_chop"
            rationale = "historical overlay context resolves to macro divergence"
        elif breakout_confirmed and volume_confirmed and trend_supportive:
            primary_regime = "breakout_trend"
            rationale = "historical overlay context resolves to breakout continuation"
        else:
            primary_regime = "quiet_range"
            rationale = "historical overlay context resolves to a quiet non-trending regime"
        return _build_classification(
            primary_regime,
            rationale=rationale,
            base_tags=existing_tags,
            macro_alignment=resolved_macro_alignment,
            macro_state=macro_state,
        )

    if strategy_id == "funding_arbitrage":
        if resolved_macro_alignment in {"divergent", "cautious", "blocked"}:
            primary_regime = "macro_divergent_chop"
            rationale = "historical carry context resolves to macro divergence"
        else:
            primary_regime = "funding_rich_carry"
            rationale = "historical carry context resolves to a funding-led regime"
        return _build_classification(
            primary_regime,
            rationale=rationale,
            base_tags=existing_tags,
            macro_alignment=resolved_macro_alignment,
            macro_state=macro_state,
        )

    if strategy_id == "atr_grid":
        if "high_volatility" in existing_tags or _resolved_macro_regime(macro_state) == "high_volatility_stress":
            primary_regime = "high_volatility_stress"
            rationale = "historical grid context resolves to volatility stress"
        elif "range_bound" in existing_tags:
            primary_regime = "quiet_range"
            rationale = "historical grid context resolves to a quiet range"
        else:
            primary_regime = "macro_divergent_chop"
            rationale = "historical grid context resolves to trend or macro conflict"
        return _build_classification(
            primary_regime,
            rationale=rationale,
            base_tags=existing_tags,
            macro_alignment=resolved_macro_alignment,
            macro_state=macro_state,
        )

    return classify_no_trade_regime(
        blockers=selected_strategy.get("blockers") or (),
        macro_alignment=resolved_macro_alignment,
        regime_tags=existing_tags,
        macro_state=macro_state,
    )