from __future__ import annotations

from decimal import Decimal
import hashlib
import json
from typing import Any, Mapping


def _decimal_text(value: Any) -> str:
    decimal_value = Decimal(str(value or "0").strip())
    if decimal_value == decimal_value.to_integral():
        return str(decimal_value.quantize(Decimal("1")))
    return format(decimal_value.normalize(), "f")


def material_fingerprint(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(dict(payload), sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16].upper()


def build_doge_entry_premium_payload(
    *,
    symbol: str,
    timeframe: str,
    signal: Any,
    contextual_signals: Mapping[str, Any],
    exchange_preview: Mapping[str, Any],
    notional_usd: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    leverage: Decimal,
    base_rationale: str,
    market_summary: str,
    gemini_lite_assessment: Mapping[str, Any] | None,
    proposal_payload: Mapping[str, Any],
    evidence_id: str,
    macro_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_kind": "entry",
        "symbol": str(symbol or "").strip().upper(),
        "timeframe": str(timeframe or "15m").strip() or "15m",
        "signal": signal.to_dict(),
        "contextual_signals": {
            str(context_timeframe or "").strip(): context_signal.to_dict()
            for context_timeframe, context_signal in contextual_signals.items()
            if str(context_timeframe or "").strip()
        },
        "exchange_preview": dict(exchange_preview or {}),
        "risk_plan": {
            "notional_usd": _decimal_text(notional_usd),
            "stop_loss_pct": _decimal_text(stop_loss_pct),
            "take_profit_pct": _decimal_text(take_profit_pct),
            "leverage": _decimal_text(leverage),
        },
        "base_rationale": str(base_rationale or "").strip(),
        "market_summary": str(market_summary or "").strip(),
        "gemini_lite_assessment": dict(gemini_lite_assessment or {}),
        "proposal_payload": dict(proposal_payload or {}),
        "evidence_id": str(evidence_id or "").strip().upper(),
        "macro_state": dict(macro_state or {}),
        "macro_state": dict(macro_state or {}),
        "macro_state": dict(macro_state or {}),
    }


def build_doge_adjustment_premium_payload(snapshot: Any, *, timeframe: str, macro_state: Mapping[str, Any] | None = None) -> dict[str, Any]:
    current_stop_price = ""
    current_take_profit_price = ""
    if snapshot.protective_orders.get("stop_loss"):
        current_stop_price = _decimal_text(snapshot.protective_orders.get("stop_loss_price") or "0")
    if snapshot.protective_orders.get("take_profit"):
        current_take_profit_price = _decimal_text(snapshot.protective_orders.get("take_profit_price") or "0")
    suggested_stop_price = _decimal_text(snapshot.recommended_stop_price)
    suggested_take_profit_price = _decimal_text(snapshot.recommended_take_profit_price)
    high_risk = False
    high_risk_reason = ""
    if current_stop_price:
        current_stop_decimal = Decimal(current_stop_price)
        suggested_stop_decimal = Decimal(suggested_stop_price)
        if snapshot.entry_side == "BUY" and suggested_stop_decimal < current_stop_decimal:
            high_risk = True
            high_risk_reason = "el stop sugerido queda por debajo del stop actual y amplia el riesgo real"
        elif snapshot.entry_side == "SELL" and suggested_stop_decimal > current_stop_decimal:
            high_risk = True
            high_risk_reason = "el stop sugerido queda por encima del stop actual y amplia el riesgo real"

    return {
        "event_kind": "adjustment",
        "symbol": str(snapshot.symbol or "").strip().upper(),
        "timeframe": str(timeframe or snapshot.timeframe or "15m").strip() or "15m",
        "signal": snapshot.signal.to_dict(),
        "contextual_signals": {
            str(context_timeframe or "").strip(): context_signal.to_dict()
            for context_timeframe, context_signal in snapshot.contextual_signals.items()
            if str(context_timeframe or "").strip()
        },
        "position": {
            "approval_id": snapshot.approval_id,
            "entry_side": snapshot.entry_side,
            "position_side": str(snapshot.active_position.get("side") or "LONG").strip().upper(),
            "entry_price": _decimal_text(snapshot.active_position.get("entry_price") or "0"),
            "market_price": _decimal_text(getattr(snapshot.signal, "last_close", "0")),
        },
        "adjustment_context": {
            "action": snapshot.plan.action,
            "summary": snapshot.plan.summary,
            "rationale": snapshot.plan.rationale,
            "current_stop_price": current_stop_price,
            "current_take_profit_price": current_take_profit_price,
            "suggested_stop_price": suggested_stop_price,
            "suggested_take_profit_price": suggested_take_profit_price,
            "protective_orders_missing": bool(snapshot.protective_orders_missing),
            "unrealized_pnl_usd": _decimal_text(snapshot.plan.unrealized_pnl_usd),
            "unrealized_pnl_pct": _decimal_text(snapshot.plan.pnl_pct),
            "higher_timeframe_support": int(snapshot.plan.higher_timeframe_support),
            "higher_timeframe_total": int(snapshot.plan.higher_timeframe_total),
            "high_risk": high_risk,
            "high_risk_reason": high_risk_reason,
            "macro_state": dict(macro_state or {}),
            "macro_state": dict(macro_state or {}),
            "macro_state": dict(macro_state or {}),
        },
    }


def premium_request_kind_label(request_kind: str) -> str:
    normalized = str(request_kind or "").strip().lower()
    if normalized == "entry":
        return "entrada"
    if normalized == "adjustment":
        return "ajuste"
    return normalized or "evento"