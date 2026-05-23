from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        value = default
    return Decimal(str(value).strip())


def _signal_state(signal: Any) -> dict[str, Any]:
    score = int(getattr(signal, "signal_score", 0) or 0)
    verdict = str(getattr(signal, "verdict", "") or "").strip().lower()
    last_close = _decimal(getattr(signal, "last_close", "0"))
    ema_fast = _decimal(getattr(signal, "ema_fast", "0"))
    ema_slow = _decimal(getattr(signal, "ema_slow", "0"))
    breakout = _decimal(getattr(signal, "breakout_reference", "0"))
    volume_ratio = _decimal(getattr(signal, "volume_ratio", "0"))
    supportive = verdict == "candidate_long" or (score >= 5 and last_close >= ema_slow)
    strong = supportive and score >= 6 and volume_ratio >= Decimal("1.10") and last_close >= breakout and last_close >= ema_fast
    weakening = last_close < ema_fast or score <= 3
    return {
        "score": score,
        "supportive": supportive,
        "strong": strong,
        "weakening": weakening,
        "last_close": last_close,
        "ema_fast": ema_fast,
        "ema_slow": ema_slow,
        "breakout": breakout,
    }


def _long_protective_price(entry_price: Decimal, pct: Decimal, *, purpose: str) -> Decimal:
    factor = pct / Decimal("100")
    if purpose == "stop_loss":
        return entry_price * (Decimal("1") - factor)
    return entry_price * (Decimal("1") + factor)


@dataclass(frozen=True)
class DogeTradeManagementPlan:
    action: str
    summary: str
    rationale: str
    unrealized_pnl_usd: Decimal
    pnl_pct: Decimal
    progress_to_take_profit: Decimal
    risk_multiple: Decimal
    original_stop_price: Decimal
    original_take_profit_price: Decimal
    suggested_stop_price: Decimal
    suggested_take_profit_price: Decimal
    higher_timeframe_support: int
    higher_timeframe_total: int


def plan_doge_long_management(
    *,
    entry_price: Decimal,
    market_price: Decimal,
    quantity: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    primary_signal: Any,
    context_signals: Mapping[str, Any],
) -> DogeTradeManagementPlan:
    if entry_price <= 0:
        raise ValueError("entry_price must be greater than zero")
    if market_price <= 0:
        raise ValueError("market_price must be greater than zero")
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")

    primary = _signal_state(primary_signal)
    context_states = [_signal_state(signal) for signal in context_signals.values()]
    higher_support = sum(1 for state in context_states if state["supportive"])
    higher_total = len(context_states)
    required_support = 0 if higher_total == 0 else max(1, (higher_total + 1) // 2)

    unrealized_pnl_usd = (market_price - entry_price) * quantity
    pnl_pct = ((market_price - entry_price) / entry_price) * Decimal("100")
    progress_to_take_profit = Decimal("0")
    if take_profit_pct > 0:
        progress_to_take_profit = pnl_pct / take_profit_pct
    risk_multiple = Decimal("0")
    if stop_loss_pct > 0:
        risk_multiple = pnl_pct / stop_loss_pct

    original_stop_price = _long_protective_price(entry_price, stop_loss_pct, purpose="stop_loss")
    original_take_profit_price = _long_protective_price(entry_price, take_profit_pct, purpose="take_profit")
    suggested_stop_price = original_stop_price
    suggested_take_profit_price = original_take_profit_price

    if market_price <= original_stop_price or (
        not primary["supportive"]
        and primary["last_close"] < primary["ema_slow"]
        and higher_support == 0
        and pnl_pct <= 0
    ):
        action = "exit_defensive"
        summary = "cierre defensivo sugerido"
        rationale = "Perdio estructura 15m y ya no tiene soporte suficiente en marcos superiores."
    elif pnl_pct <= 0:
        if primary["weakening"] and higher_support == 0:
            action = "hold_defensive"
            summary = "mantener defensa; no subir niveles aun"
            rationale = "La posicion sigue sin pagar riesgo y la estructura corta se debilita."
        else:
            action = "hold"
            summary = "mantener plan original"
            rationale = "La posicion aun no ha recorrido suficiente distancia como para mover el piso con ventaja."
    elif progress_to_take_profit >= Decimal("0.85"):
        if primary["strong"] and higher_support >= required_support:
            suggested_stop_price = max(
                original_stop_price,
                entry_price,
                entry_price + ((market_price - entry_price) * Decimal("0.55")),
            )
            suggested_take_profit_price = max(
                original_take_profit_price,
                entry_price + ((original_take_profit_price - entry_price) * Decimal("1.35")),
            )
            action = "trail_and_extend"
            summary = "subir SL para asegurar ganancia y extender TP"
            rationale = (
                "La jugada ya esta muy cerca del objetivo y la estructura sigue fuerte en 15m "
                "con apoyo superior suficiente."
            )
        else:
            suggested_stop_price = max(
                original_stop_price,
                entry_price,
                entry_price + ((market_price - entry_price) * Decimal("0.40")),
            )
            action = "trail_profit"
            summary = "subir SL para asegurar buena parte del beneficio"
            rationale = "La jugada ya recorrio casi todo el objetivo; conviene defender beneficio antes que ampliar riesgo."
    elif progress_to_take_profit >= Decimal("0.40"):
        suggested_stop_price = max(original_stop_price, entry_price)
        if primary["supportive"] and higher_support >= required_support:
            action = "raise_stop_breakeven"
            summary = "subir SL a break-even"
            rationale = "La posicion ya pago suficiente riesgo y se mantiene por encima de la estructura corta clave."
        else:
            action = "tighten_stop"
            rationale = "La posicion avanza, pero la confirmacion superior ya no es tan limpia; conviene apretar el piso."
            if market_price > entry_price:
                suggested_stop_price = max(
                    suggested_stop_price,
                    entry_price + ((market_price - entry_price) * Decimal("0.20")),
                )
            summary = "apretar SL y conservar el TP actual"
    elif primary["weakening"] and pnl_pct > 0:
        suggested_stop_price = max(
            original_stop_price,
            entry_price + ((market_price - entry_price) * Decimal("0.20")),
        )
        action = "tighten_stop"
        summary = "apretar SL por debilitamiento intradia"
        rationale = "Hay ganancia abierta, pero 15m empezo a ceder momentum; mejor defender antes que devolverla completa."
    else:
        action = "hold"
        summary = "mantener plan original"
        rationale = "La estructura sigue viva, pero todavia no hay desplazamiento suficiente para reanclar niveles con ventaja."

    if suggested_stop_price >= market_price:
        suggested_stop_price = entry_price if entry_price < market_price else original_stop_price
    if suggested_take_profit_price <= market_price:
        suggested_take_profit_price = original_take_profit_price

    return DogeTradeManagementPlan(
        action=action,
        summary=summary,
        rationale=rationale,
        unrealized_pnl_usd=unrealized_pnl_usd,
        pnl_pct=pnl_pct,
        progress_to_take_profit=progress_to_take_profit,
        risk_multiple=risk_multiple,
        original_stop_price=original_stop_price,
        original_take_profit_price=original_take_profit_price,
        suggested_stop_price=suggested_stop_price,
        suggested_take_profit_price=suggested_take_profit_price,
        higher_timeframe_support=higher_support,
        higher_timeframe_total=higher_total,
    )

def _short_protective_price(entry_price: Decimal, pct: Decimal, *, purpose: str) -> Decimal:
    factor = pct / Decimal("100")
    if purpose == "stop_loss":
        return entry_price * (Decimal("1") + factor)
    return entry_price * (Decimal("1") - factor)

def plan_doge_short_management(
    *,
    entry_price: Decimal,
    market_price: Decimal,
    quantity: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    primary_signal: Any,
    context_signals: Mapping[str, Any],
) -> DogeTradeManagementPlan:
    if entry_price <= 0:
        raise ValueError("entry_price must be greater than zero")
    if market_price <= 0:
        raise ValueError("market_price must be greater than zero")
    if quantity <= 0:
        raise ValueError("quantity must be greater than zero")

    primary = _signal_state(primary_signal)
    context_states = [_signal_state(signal) for signal in context_signals.values()]
    # Downward support means weakening in long terms, so we invert supportive logic
    higher_support = sum(1 for state in context_states if state["weakening"] or (state["score"] <= 4 and state["last_close"] < state["ema_slow"]))
    higher_total = len(context_states)
    required_support = 0 if higher_total == 0 else max(1, (higher_total + 1) // 2)

    unrealized_pnl_usd = (entry_price - market_price) * quantity
    pnl_pct = ((entry_price - market_price) / entry_price) * Decimal("100")
    progress_to_take_profit = Decimal("0")
    if take_profit_pct > 0:
        progress_to_take_profit = pnl_pct / take_profit_pct
    risk_multiple = Decimal("0")
    if stop_loss_pct > 0:
        risk_multiple = pnl_pct / stop_loss_pct

    original_stop_price = _short_protective_price(entry_price, stop_loss_pct, purpose="stop_loss")
    original_take_profit_price = _short_protective_price(entry_price, take_profit_pct, purpose="take_profit")
    suggested_stop_price = original_stop_price
    suggested_take_profit_price = original_take_profit_price

    if market_price >= original_stop_price or (
        primary["supportive"]
        and primary["last_close"] > primary["ema_slow"]
        and higher_support == 0
        and pnl_pct <= 0
    ):
        action = "exit_defensive"
        summary = "cierre defensivo sugerido"
        rationale = "Perdio estructura corta 15m y temporalidades superiores muestran soporte al alza."
    elif pnl_pct <= 0:
        if primary["supportive"] and higher_support == 0:
            action = "hold_defensive"
            summary = "mantener defensa; no bajar niveles aun"
            rationale = "La posicion sigue sin pagar riesgo y la estructura bajista no se afianza."
        else:
            action = "hold"
            summary = "mantener plan original"
            rationale = "La posicion aun no recorrido suficiente distancia a la baja como para mover el techo con ventaja."
    elif progress_to_take_profit >= Decimal("0.85"):
        if primary["weakening"] and higher_support >= required_support:
            suggested_stop_price = min(
                original_stop_price,
                entry_price,
                entry_price - ((entry_price - market_price) * Decimal("0.55")),
            )
            suggested_take_profit_price = min(
                original_take_profit_price,
                entry_price - ((entry_price - original_take_profit_price) * Decimal("1.35")),
            )
            action = "trail_and_extend"
            summary = "bajar SL para asegurar ganancia y extender TP"
            rationale = "La jugada ya esta muy cerca del objetivo bajista y la estructura sigue cediendo en 15m con apoyo superior."
        else:
            suggested_stop_price = min(
                original_stop_price,
                entry_price,
                entry_price - ((entry_price - market_price) * Decimal("0.40")),
            )
            action = "trail_profit"
            summary = "bajar SL para asegurar buena parte del beneficio"
            rationale = "La jugada ya recorrio casi todo el objetivo a la baja; conviene defender beneficio."
    elif progress_to_take_profit >= Decimal("0.40"):
        suggested_stop_price = min(original_stop_price, entry_price)
        if primary["weakening"] and higher_support >= required_support:
            action = "raise_stop_breakeven"
            summary = "bajar SL a break-even"
            rationale = "La posicion corta ya pago suficiente riesgo."
        else:
            action = "tighten_stop"
            rationale = "La posicion avanza a la baja, pero sin confirmacion limpia; conviene apretar el techo."
            if market_price < entry_price:
                suggested_stop_price = min(
                    suggested_stop_price,
                    entry_price - ((entry_price - market_price) * Decimal("0.20")),
                )
            summary = "apretar SL y conservar el TP actual"
    elif primary["supportive"] and pnl_pct > 0:
        suggested_stop_price = min(
            original_stop_price,
            entry_price - ((entry_price - market_price) * Decimal("0.20")),
        )
        action = "tighten_stop"
        summary = "apretar SL por rebote intradia"
        rationale = "Hay ganancia abierta, pero 15m muestra rebote; mejor defender antes que devolverla."
    else:
        action = "hold"
        summary = "mantener plan original"
        rationale = "La estructura bajista sigue viva, mantener plan original."

    if suggested_stop_price <= market_price:
        suggested_stop_price = entry_price if entry_price > market_price else original_stop_price
    if suggested_take_profit_price >= market_price:
        suggested_take_profit_price = original_take_profit_price

    return DogeTradeManagementPlan(
        action=action,
        summary=summary,
        rationale=rationale,
        unrealized_pnl_usd=unrealized_pnl_usd,
        pnl_pct=pnl_pct,
        progress_to_take_profit=progress_to_take_profit,
        risk_multiple=risk_multiple,
        original_stop_price=original_stop_price,
        original_take_profit_price=original_take_profit_price,
        suggested_stop_price=suggested_stop_price,
        suggested_take_profit_price=suggested_take_profit_price,
        higher_timeframe_support=higher_support,
        higher_timeframe_total=higher_total,
    )

def plan_doge_management(
    *,
    entry_side: str,
    entry_price: Decimal,
    market_price: Decimal,
    quantity: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    primary_signal: Any,
    context_signals: Mapping[str, Any],
) -> DogeTradeManagementPlan:
    if str(entry_side or "").strip().upper() == "SELL":
        return plan_doge_short_management(
            entry_price=entry_price,
            market_price=market_price,
            quantity=quantity,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            primary_signal=primary_signal,
            context_signals=context_signals,
        )
    return plan_doge_long_management(
        entry_price=entry_price,
        market_price=market_price,
        quantity=quantity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        primary_signal=primary_signal,
        context_signals=context_signals,
    )
