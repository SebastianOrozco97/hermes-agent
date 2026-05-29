"""Guarded Binance MCP server.

This server intentionally starts with a paper-first surface. Every trade
proposal is validated against the local risk policy before an execution
envelope is returned. Live order routing remains blocked until a dedicated
exchange adapter is added on top of this policy layer.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal
import json
import logging
import os
import sys
import urllib.request
from typing import Any, Optional

from hermes_constants import get_env_path
from tools.binance_live_adapter import (
    BinanceFuturesLiveExecutor,
    BinanceLiveExecutionError,
)
from tools.execution_orchestrators import execute_arbitrage, execute_grid, reconcile_grid
from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage
from tools.doge_grid_advisor import plan_dynamic_grid

from tools.binance_guardrails import (
    BinanceAccountSnapshot,
    BinanceRiskLimits,
    BinanceTradeProposal,
    _parse_bool,
    evaluate_trade_proposal,
    get_strategy_leverage_cap,
    get_kill_switch_path,
    is_kill_switch_active,
    set_kill_switch,
)
from tools.binance_paper_runtime import (
    close_paper_position,
    complete_doge_premium_analysis_request,
    consume_trade_approval,
    get_doge_premium_analysis_request,
    get_latest_doge_premium_analysis_request,
    get_paper_daily_summary,
    get_open_paper_position,
    get_paper_position_status,
    get_paper_account_overview,
    get_latest_trade_approval,
    get_trade_approval,
    open_paper_position,
    record_live_trade_execution_failure,
    record_live_trade_execution_success,
    record_live_trade_protection_adjustment,
    record_market_evidence,
    record_doge_premium_analysis_decision,
    record_trade_approval,
    reconcile_protective_exits,
    request_trade_approval,
    seed_paper_account,
    validate_trade_approval,
)
from tools.doge_live_manager import build_doge_live_management_snapshot
from tools.doge_premium_flow import (
    build_doge_adjustment_premium_payload,
    material_fingerprint,
    premium_request_kind_label,
)
from tools.doge_premium_gemini_verifier import (
    DogePremiumGeminiVerifierError,
    verify_doge_adjustment_with_premium_gemini,
    verify_doge_entry_with_premium_gemini,
)

logger = logging.getLogger(__name__)
_LOADED_ENV_PATH: Optional[str] = None
_LOADED_ENV_MTIME_NS: Optional[int] = None


def _ensure_runtime_env_loaded() -> None:
    global _LOADED_ENV_PATH, _LOADED_ENV_MTIME_NS

    env_path = get_env_path()
    env_key = str(env_path)
    env_mtime_ns: Optional[int] = None
    if env_path.exists():
        try:
            env_mtime_ns = env_path.stat().st_mtime_ns
        except OSError:
            env_mtime_ns = None

    if _LOADED_ENV_PATH == env_key and _LOADED_ENV_MTIME_NS == env_mtime_ns:
        return

    try:
        from dotenv import load_dotenv
    except Exception:
        _LOADED_ENV_PATH = env_key
        _LOADED_ENV_MTIME_NS = env_mtime_ns
        return

    if env_path.exists():
        try:
            load_dotenv(str(env_path), override=True, encoding="utf-8")
        except UnicodeDecodeError:
            load_dotenv(str(env_path), override=True, encoding="latin-1")
    _LOADED_ENV_PATH = env_key
    _LOADED_ENV_MTIME_NS = env_mtime_ns


def _resolve_requested_trade_mode(
    mode: str,
    *,
    active_limits: Optional[BinanceRiskLimits] = None,
) -> str:
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"", "auto"}:
        return normalized_mode

    limits = active_limits
    if limits is None:
        _ensure_runtime_env_loaded()
        limits = BinanceRiskLimits.from_env()
    return limits.mode


def _resolve_effective_risk_limits(mode: str) -> tuple[str, BinanceRiskLimits, BinanceRiskLimits]:
    _ensure_runtime_env_loaded()
    active_limits = BinanceRiskLimits.from_env()
    resolved_mode = _resolve_requested_trade_mode(mode, active_limits=active_limits)
    if resolved_mode == active_limits.mode:
        return resolved_mode, active_limits, active_limits
    if resolved_mode == "paper":
        return resolved_mode, replace(active_limits, mode="paper", live_trading_enabled=False), active_limits
    return resolved_mode, active_limits, active_limits


def _get_live_executor(*, require_credentials: bool = True) -> BinanceFuturesLiveExecutor:
    _ensure_runtime_env_loaded()
    return BinanceFuturesLiveExecutor.from_env(require_credentials=require_credentials)


def _price_result(symbol: str) -> dict[str, Any]:
    normalized_symbol = str(symbol).strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol is required"}
    try:
        executor = _get_live_executor(require_credentials=False)
        price = executor.get_reference_price(normalized_symbol)
    except BinanceLiveExecutionError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "symbol": normalized_symbol,
        "reference_price": format(price.normalize(), "f"),
    }


def _decimal_text(value: Any, default: str = "0") -> str:
    try:
        decimal_value = Decimal(str(value).strip())
    except Exception:
        decimal_value = Decimal(default)
    if decimal_value == decimal_value.to_integral():
        return format(decimal_value.quantize(Decimal("1")), "f")
    return format(decimal_value.normalize(), "f")


def _format_fixed_decimal(value: Any, places: int, default: str = "0") -> str:
    try:
        decimal_value = Decimal(str(value).strip())
    except Exception:
        decimal_value = Decimal(default)
    quantum = Decimal("1").scaleb(-places)
    return format(decimal_value.quantize(quantum), f".{places}f")


def _format_price_text(value: Any) -> str:
    try:
        decimal_value = Decimal(str(value).strip())
    except Exception:
        decimal_value = Decimal("0")
    absolute = abs(decimal_value)
    if absolute >= Decimal("1000"):
        places = 2
    elif absolute >= Decimal("1"):
        places = 4
    else:
        places = 6
    return _format_fixed_decimal(decimal_value, places)


def _format_usd_text(value: Any) -> str:
    return _format_fixed_decimal(value, 2)


def _format_pct_text(value: Any) -> str:
    return _format_fixed_decimal(value, 2)


def _operator_timestamp(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "n/d"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _operator_exchange_timestamp(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "n/d"
    try:
        milliseconds = int(text)
    except ValueError:
        return _operator_timestamp(text)
    if milliseconds <= 0:
        return "n/d"
    parsed = datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc)
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")


def _proposal_risk_snapshot(notional_usd: float, stop_loss_pct: float, take_profit_pct: float) -> dict[str, Any]:
    notional = Decimal(str(notional_usd))
    stop_loss = Decimal(str(stop_loss_pct or 0))
    take_profit = Decimal(str(take_profit_pct or 0))
    estimated_max_loss_usd = (notional * stop_loss) / Decimal("100")
    estimated_max_profit_usd = (notional * take_profit) / Decimal("100")
    risk_reward_ratio: Optional[str] = None
    if estimated_max_loss_usd > 0 and estimated_max_profit_usd > 0:
        risk_reward_ratio = _decimal_text(estimated_max_profit_usd / estimated_max_loss_usd)
    return {
        "notional_usd": _decimal_text(notional),
        "estimated_max_loss_usd": _decimal_text(estimated_max_loss_usd),
        "estimated_max_profit_usd": _decimal_text(estimated_max_profit_usd),
        "risk_reward_ratio": risk_reward_ratio,
    }


def _format_follow_up_line(commands: dict[str, Any], *, include_close: bool = True) -> Optional[str]:
    parts: list[str] = []
    trade_status = str(commands.get("status_trade", "") or "").strip()
    position_status = str(commands.get("status_position", "") or "").strip()
    close_position = str(commands.get("close_position", "") or "").strip()
    if trade_status:
        parts.append(trade_status)
    elif position_status:
        parts.append(position_status)
    if include_close and close_position:
        parts.append(close_position)
    if not parts:
        return None
    return "Seguimiento: " + " | ".join(parts)


def _trigger_label(trigger: str) -> str:
    normalized = str(trigger or "").strip().lower()
    if normalized == "take_profit":
        return "TP"
    if normalized == "stop_loss":
        return "SL"
    if normalized == "manual":
        return "manual"
    return normalized or "n/d"


def _build_paper_approval_whatsapp_message(
    *,
    approval_id: str,
    symbol: str,
    side: str,
    notional_usd: float,
    stop_loss_pct: float,
    take_profit_pct: float,
    expires_at: str = "",
    symbol_shortcut: str = "",
) -> str:
    risk = _proposal_risk_snapshot(notional_usd, stop_loss_pct, take_profit_pct)
    normalized_shortcut = str(symbol_shortcut or "").strip().upper()
    lines = [
        f"Aprobacion requerida {approval_id} | {side.upper()} {symbol.upper()}",
        (
            f"Notional {risk['notional_usd']} USD | Riesgo {_format_usd_text(risk['estimated_max_loss_usd'])} USD | "
            f"SL {_format_pct_text(stop_loss_pct)}% | TP {_format_pct_text(take_profit_pct)}%"
        ),
    ]
    expiry_label = _operator_timestamp(expires_at)
    if expiry_label != "n/d":
        lines.append(f"Expira {expiry_label}")
    if normalized_shortcut:
        lines.append(
            f"Comandos: APROBAR {normalized_shortcut} | RECHAZAR {normalized_shortcut} | ESTADO {normalized_shortcut}"
        )
        lines.append(f"Exacto: APROBAR {approval_id} | RECHAZAR {approval_id} | ESTADO {approval_id}")
    else:
        lines.append(f"Comandos: APROBAR {approval_id} | RECHAZAR {approval_id} | ESTADO {approval_id}")
    return "\n".join(lines)


def _build_paper_entry_whatsapp_message(execution: dict[str, Any]) -> str:
    position = execution.get("position") or {}
    risk = execution.get("risk") or {}
    commands = execution.get("commands") or {}
    lines = [
        f"Paper ejecutado {position.get('side')} {position.get('symbol')} | {position.get('position_id')} | {position.get('approval_id') or 'sin approval id'}",
        f"Fill: {_operator_timestamp(str(execution.get('filled_at', '') or position.get('opened_at', '') or ''))} a {_format_price_text(execution.get('fill_reference_price') or position.get('entry_price') or '0')}",
        f"Notional {_format_usd_text(risk.get('notional_usd') or position.get('notional_usd') or '0')} USD | Riesgo max {_format_usd_text(risk.get('estimated_max_loss_usd') or '0')} USD | R/B {risk.get('risk_reward_ratio') or 'n/d'}",
        f"SL {_format_price_text(risk.get('stop_loss_price') or position.get('stop_loss_price') or '0')} | TP {_format_price_text(risk.get('take_profit_price') or position.get('take_profit_price') or '0')}",
    ]
    follow_up = _format_follow_up_line(commands)
    if follow_up:
        lines.append(follow_up)
    return "\n".join(lines)


def _build_live_entry_whatsapp_message(result: dict[str, Any]) -> str:
    execution = result.get("execution") or {}
    decision = result.get("decision") or {}
    proposal = decision.get("proposal") or {}
    approval = result.get("approval") or {}
    entry_order = execution.get("entry_order") or {}
    protective_orders = execution.get("protective_orders") or {}
    approval_id = str(approval.get("approval_id") or "").strip() or "sin approval id"
    symbol = str(proposal.get("symbol") or "").strip().upper()
    symbol_shortcut = symbol.removesuffix("USDT") if symbol.endswith("USDT") else symbol
    lines = [
        f"Live ejecutado {proposal.get('side')} {proposal.get('symbol')} | {approval_id}",
        (
            f"Fill {_format_price_text(entry_order.get('avgPrice') or execution.get('entry_price') or execution.get('reference_price') or '0')}"
            f" | Qty {_decimal_text(execution.get('quantity') or entry_order.get('executedQty') or '0')}"
            f" | Estado {entry_order.get('status') or 'n/d'}"
        ),
        (
            f"Notional {_format_usd_text(proposal.get('notional_usd') or '0')} USD | "
            f"Lev {proposal.get('leverage') or '1'} | "
            f"Hora {_operator_exchange_timestamp(entry_order.get('updateTime') or entry_order.get('transactTime') or entry_order.get('time') or '')}"
        ),
        (
            f"SL {_format_price_text(protective_orders.get('stop_loss_price') or '0')} | "
            f"TP {_format_price_text(protective_orders.get('take_profit_price') or '0')}"
        ),
        (
            "Seguimiento: esperar radar 15m"
            + (f" | AJUSTAR {symbol_shortcut} cuando Hermes lo pida" if symbol_shortcut else "")
        ),
    ]
    return "\n".join(lines)


def _symbol_shortcut(symbol: str) -> str:
    normalized = str(symbol or "").strip().upper()
    if normalized.endswith("USDT"):
        return normalized[:-4]
    return normalized


def _display_price_text(value: Any) -> str:
    if str(value or "").strip() == "":
        return "n/d"
    return _format_price_text(value)


def _premium_model_display_name(model: str) -> str:
    normalized = str(model or "").strip().lower()
    if normalized.startswith("gemini-3.5-flash"):
        return "Gemini 3.5 Flash"
    return str(model or "Gemini premium").strip() or "Gemini premium"


def _doge_premium_analysis_enabled() -> bool:
    _ensure_runtime_env_loaded()
    return _parse_bool(os.getenv("DOGE_PREMIUM_ANALYSIS_ENABLED"), default=True)


def _doge_premium_analysis_model() -> str:
    _ensure_runtime_env_loaded()
    return str(os.getenv("DOGE_PREMIUM_ANALYSIS_MODEL", "gemini-3.5-flash") or "").strip() or "gemini-3.5-flash"


def _doge_premium_analysis_timeout_sec() -> float:
    _ensure_runtime_env_loaded()
    raw = str(os.getenv("DOGE_PREMIUM_ANALYSIS_TIMEOUT_SEC", "90") or "90").strip() or "90"
    try:
        return max(30.0, float(raw))
    except ValueError:
        return 90.0


def _build_doge_premium_request_whatsapp_message(request: dict[str, Any]) -> str:
    material_payload = request.get("material_payload") or {}
    request_kind = str(request.get("request_kind") or "").strip().lower()
    symbol = str(request.get("symbol") or "DOGEUSDT").strip().upper() or "DOGEUSDT"
    kind_label = premium_request_kind_label(request_kind)
    model_label = _premium_model_display_name(str(request.get("model") or ""))
    
    # 4. Transformacion de Salida hacia WhatsApp - Semaforo Macro
    macro_state = material_payload.get("macro_state") or {}
    macro_semaphore = ""
    if macro_state:
        btc_trend = macro_state.get("btc_trend_1h", "neutral")
        side = "BUY"
        if request_kind == "entry":
            proposal = material_payload.get("proposal_payload") or {}
            side = str(proposal.get("side") or "BUY").strip().upper()
        if side == "BUY" and btc_trend == "bullish":
            macro_semaphore = " ?? MACRO ALINEADO (BTC impulsando)"
        elif side == "SELL" and btc_trend == "bearish":
            macro_semaphore = " ?? MACRO ALINEADO (BTC impulsando bajada)"
        elif side == "BUY" and btc_trend == "bearish":
            macro_semaphore = " ?? MACRO OPUESTO (BTC arrastre bajista)"
        elif side == "SELL" and btc_trend == "bullish":
            macro_semaphore = " ?? MACRO OPUESTO (BTC divergencia alcista)"
        else:
            macro_semaphore = " ?? MACRO NEUTRAL / LATERAL"
            
    lines = [f"Analisis premium pendiente {symbol} | {kind_label} | {model_label}{macro_semaphore}"]
    if request_kind == "entry":
        base_summary = str(request.get("material_summary") or material_payload.get("market_summary") or "").strip()
        if base_summary:
            lines.append(f"Base: {base_summary}")
    elif request_kind == "adjustment":
        adjustment_context = material_payload.get("adjustment_context") or {}
        lines.append(f"Base: {adjustment_context.get('summary') or request.get('material_summary') or 'ajuste accionable'}")
        if adjustment_context.get("high_risk"):
            lines.append(
                "Riesgo: ALTO RIESGO. " + str(adjustment_context.get("high_risk_reason") or "amplia el riesgo real")
            )
    expiry_label = _operator_timestamp(str(request.get("expires_at") or ""))
    if expiry_label != "n/d":
        lines.append(f"Expira {expiry_label}")
    lines.append("Comandos: ANALIZAR DOGE | RECHAZAR ANALISIS DOGE | ESTADO DOGE")
    return "\n".join(lines)


def _build_doge_adjustment_ready_message(
    material_payload: dict[str, Any],
    *,
    intro: str = "",
    premium_assessment: Optional[dict[str, Any]] = None,
) -> str:
    adjustment_context = material_payload.get("adjustment_context") or {}
    position = material_payload.get("position") or {}
    lines: list[str] = []
    if intro:
        lines.append(intro)
    lines.append(
        f"DOGE ajuste listo | {str(position.get('approval_id') or 'sin approval id').strip().upper() or 'sin approval id'}"
    )
    lines.append(
        (
            f"Mercado {_format_price_text(position.get('market_price') or '0')} | "
            f"PnL {_format_usd_text(adjustment_context.get('unrealized_pnl_usd') or '0')} USD "
            f"({_format_pct_text(adjustment_context.get('unrealized_pnl_pct') or '0')}%)"
        )
    )
    lines.append(
        (
            f"SL {_display_price_text(adjustment_context.get('current_stop_price'))} -> {_display_price_text(adjustment_context.get('suggested_stop_price'))} | "
            f"TP {_display_price_text(adjustment_context.get('current_take_profit_price'))} -> {_display_price_text(adjustment_context.get('suggested_take_profit_price'))}"
        )
    )
    lines.append(f"Plan: {adjustment_context.get('summary') or 'ajuste accionable'}")
    if adjustment_context.get("high_risk"):
        lines.append(
            "Riesgo: ALTO RIESGO. " + str(adjustment_context.get("high_risk_reason") or "amplia el riesgo real")
        )
    if premium_assessment:
        risk_flags = premium_assessment.get("risk_flags") or []
        lines.append(
            f"Gemini 3.5 Flash: {premium_assessment.get('summary') or 'confirma el ajuste'} | Conf {_format_pct_text(Decimal(str(premium_assessment.get('confidence') or '0')) * Decimal('100'))}%"
        )
        if premium_assessment.get("suggested_stop_price") or premium_assessment.get("suggested_take_profit_price"):
            lines.append(
                (
                    f"Premium sugiere: SL {_display_price_text(premium_assessment.get('suggested_stop_price'))} | "
                    f"TP {_display_price_text(premium_assessment.get('suggested_take_profit_price'))}"
                )
            )
        if premium_assessment.get("risk_label") == "alto_riesgo":
            lines.append("Etiqueta premium: ALTO RIESGO")
        if risk_flags:
            lines.append("Riesgos: " + ", ".join(str(item) for item in risk_flags))
    lines.append("Seguimiento: AJUSTAR DOGE")
    return "\n".join(lines)


def _proposal_payload_matches(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = (
        "symbol",
        "side",
        "mode",
        "order_type",
        "notional_usd",
        "stop_loss_pct",
        "take_profit_pct",
        "leverage",
        "verifier_model",
    )
    return all(str(left.get(key) or "") == str(right.get(key) or "") for key in keys)


def _ensure_trade_approval_from_premium_request(
    request: dict[str, Any],
    *,
    requested_via: str,
) -> dict[str, Any]:
    material_payload = request.get("material_payload") or {}
    proposal_payload = material_payload.get("proposal_payload") or {}
    proposal = BinanceTradeProposal.from_payload(proposal_payload)
    pending_approval = get_latest_trade_approval(symbol=proposal.symbol, status="pending")
    if pending_approval is not None and _proposal_payload_matches(pending_approval.get("proposal") or {}, proposal.to_dict()):
        return pending_approval
    return request_trade_approval(
        proposal,
        evidence_id=str(material_payload.get("evidence_id") or ""),
        market_summary=str(material_payload.get("market_summary") or ""),
        requested_via=requested_via,
        decision_context=material_payload.get("decision_context") or None,
    )


def _execute_doge_premium_analysis(request: dict[str, Any]) -> dict[str, Any]:
    material_payload = request.get("material_payload") or {}
    request_kind = str(request.get("request_kind") or "").strip().lower()
    model = str(request.get("model") or _doge_premium_analysis_model()).strip() or _doge_premium_analysis_model()
    timeout = _doge_premium_analysis_timeout_sec()
    if request_kind == "entry":
        assessment = verify_doge_entry_with_premium_gemini(
            payload=material_payload,
            model=model,
            timeout=timeout,
        )
    elif request_kind == "adjustment":
        assessment = verify_doge_adjustment_with_premium_gemini(
            payload=material_payload,
            model=model,
            timeout=timeout,
        )
    else:
        raise ValueError(f"unsupported premium request kind: {request_kind}")
    return assessment.to_dict()


def _resolve_doge_premium_analysis_request(
    *,
    symbol: str,
    decision: str,
) -> dict[str, Any]:
    _ensure_runtime_env_loaded()
    normalized_symbol = str(symbol or "").strip().upper()
    pending_request = get_latest_doge_premium_analysis_request(symbol=normalized_symbol, status="pending")
    if pending_request is None:
        latest_request = get_latest_doge_premium_analysis_request(symbol=normalized_symbol)
        return {
            "success": False,
            "error": f"No hay un analisis premium pendiente para {normalized_symbol}.",
            "reason_code": "no_pending_request",
            "request": latest_request,
            "symbol": normalized_symbol,
        }

    resolved_request = record_doge_premium_analysis_decision(
        str(pending_request.get("request_id") or ""),
        decision=decision,
        response_text=f"Premium decision via WhatsApp DM: {decision}",
        responder="operator",
    )
    request_kind = str(resolved_request.get("request_kind") or "").strip().lower()

    if str(decision or "").strip().lower() in {"deny", "denied", "reject", "rejected", "cancel", "canceled", "no", "n"}:
        result = {
            "success": True,
            "premium_outcome": "denied_fallback",
            "request": resolved_request,
            "symbol": normalized_symbol,
        }
        if request_kind == "entry":
            result["trade_approval"] = _ensure_trade_approval_from_premium_request(
                resolved_request,
                requested_via="doge_premium_denied_fallback",
            )
        return result

    try:
        assessment = _execute_doge_premium_analysis(resolved_request)
    except DogePremiumGeminiVerifierError as exc:
        completed_request = complete_doge_premium_analysis_request(
            str(resolved_request.get("request_id") or ""),
            analysis_outcome="error",
            response_text=str(exc),
            responder="system",
        )
        result = {
            "success": True,
            "premium_outcome": "error_fallback",
            "error": str(exc),
            "request": completed_request,
            "symbol": normalized_symbol,
        }
        if completed_request.get("fallback_allowed") and request_kind == "entry":
            result["trade_approval"] = _ensure_trade_approval_from_premium_request(
                completed_request,
                requested_via="doge_premium_error_fallback",
            )
        return result

    premium_outcome = "passed" if assessment.get("passed") else "rejected"
    completed_request = complete_doge_premium_analysis_request(
        str(resolved_request.get("request_id") or ""),
        analysis_outcome=premium_outcome,
        analysis=assessment,
        response_text=assessment.get("summary") or "",
        responder="system",
    )
    result = {
        "success": True,
        "premium_outcome": premium_outcome,
        "assessment": assessment,
        "request": completed_request,
        "symbol": normalized_symbol,
    }
    if premium_outcome == "passed" and request_kind == "entry":
        result["trade_approval"] = _ensure_trade_approval_from_premium_request(
            completed_request,
            requested_via="doge_premium_passed",
        )
    return result


def _build_doge_premium_resolution_whatsapp_message(result: dict[str, Any]) -> str:
    request = result.get("request") or {}
    request_kind = str(request.get("request_kind") or "").strip().lower()
    request_model = _premium_model_display_name(str(request.get("model") or ""))
    assessment = result.get("assessment") or {}
    outcome = str(result.get("premium_outcome") or "").strip().lower()
    symbol = str(request.get("symbol") or result.get("symbol") or "DOGEUSDT").strip().upper() or "DOGEUSDT"

    if outcome in {"denied_fallback", "error_fallback"}:
        intro = "Analisis premium omitido; vuelvo al flujo actual con Gemini 3.1 Flash Lite."
        if outcome == "error_fallback":
            intro = (
                f"{request_model} no estuvo disponible; vuelvo al flujo actual con Gemini 3.1 Flash Lite."
            )
        if request_kind == "entry":
            approval = result.get("trade_approval") or {}
            proposal = approval.get("proposal") or {}
            body = _build_paper_approval_whatsapp_message(
                approval_id=str(approval.get("approval_id") or ""),
                symbol=str(proposal.get("symbol") or symbol),
                side=str(proposal.get("side") or "BUY"),
                notional_usd=float(proposal.get("notional_usd") or 0.0),
                stop_loss_pct=float(proposal.get("stop_loss_pct") or 0.0),
                take_profit_pct=float(proposal.get("take_profit_pct") or 0.0),
                expires_at=str(approval.get("expires_at") or ""),
                symbol_shortcut=_symbol_shortcut(str(proposal.get("symbol") or symbol)),
            )
            return intro + "\n" + body
        return _build_doge_adjustment_ready_message(
            request.get("material_payload") or {},
            intro=intro,
        )

    if outcome == "passed":
        if request_kind == "entry":
            approval = result.get("trade_approval") or {}
            proposal = approval.get("proposal") or {}
            intro_lines = [
                f"{request_model} confirma entrada {symbol} | Conf {_format_pct_text(Decimal(str(assessment.get('confidence') or '0')) * Decimal('100'))}%",
                f"Resumen: {assessment.get('summary') or 'setup valido'}",
            ]
            risk_flags = assessment.get("risk_flags") or []
            if assessment.get("suggested_stop_price") or assessment.get("suggested_take_profit_price"):
                intro_lines.append(
                    (
                        f"Premium sugiere: SL {_display_price_text(assessment.get('suggested_stop_price'))} | "
                        f"TP {_display_price_text(assessment.get('suggested_take_profit_price'))}"
                    )
                )
            if risk_flags:
                intro_lines.append("Riesgos: " + ", ".join(str(item) for item in risk_flags))
            intro_lines.append(f"Operador: {assessment.get('operator_note') or 'n/d'}")
            body = _build_paper_approval_whatsapp_message(
                approval_id=str(approval.get("approval_id") or ""),
                symbol=str(proposal.get("symbol") or symbol),
                side=str(proposal.get("side") or "BUY"),
                notional_usd=float(proposal.get("notional_usd") or 0.0),
                stop_loss_pct=float(proposal.get("stop_loss_pct") or 0.0),
                take_profit_pct=float(proposal.get("take_profit_pct") or 0.0),
                expires_at=str(approval.get("expires_at") or ""),
                symbol_shortcut=_symbol_shortcut(str(proposal.get("symbol") or symbol)),
            )
            return "\n".join(intro_lines + [body])
        intro = f"{request_model} valida el ajuste DOGE | Conf {_format_pct_text(Decimal(str(assessment.get('confidence') or '0')) * Decimal('100'))}%"
        return _build_doge_adjustment_ready_message(
            request.get("material_payload") or {},
            intro=intro,
            premium_assessment=assessment,
        )

    if outcome == "rejected":
        lines = [
            f"{request_model} descarta {premium_request_kind_label(request_kind)} {symbol}.",
            f"Resumen: {assessment.get('summary') or 'n/d'}",
        ]
        risk_flags = assessment.get("risk_flags") or []
        if risk_flags:
            lines.append("Riesgos: " + ", ".join(str(item) for item in risk_flags))
        lines.append(f"Operador: {assessment.get('operator_note') or 'n/d'}")
        lines.append("Seguimiento: esperar siguiente radar 15m")
        return "\n".join(lines)

    return result.get("error") or f"No hay un resultado premium accionable para {symbol}."


def _build_doge_premium_status_whatsapp_message(request: dict[str, Any]) -> str:
    status = str(request.get("status") or "").strip().lower()
    if status == "pending":
        return _build_doge_premium_request_whatsapp_message(request)
    if status == "completed":
        return _build_doge_premium_resolution_whatsapp_message(
            {
                "premium_outcome": str(request.get("analysis_outcome") or "").strip().lower(),
                "request": request,
                "assessment": request.get("analysis") or {},
                "symbol": request.get("symbol"),
            }
        )
    label = {
        "approved": "aprobado y en proceso",
        "denied": "omitido por operador",
        "expired": "expirado",
    }.get(status, status or "desconocido")
    return (
        f"Analisis premium {str(request.get('symbol') or 'DOGEUSDT').strip().upper()} {label}.\n"
        "Seguimiento: ESTADO DOGE"
    )


def _management_result_payload(snapshot: Any) -> dict[str, Any]:
    return {
        "symbol": snapshot.symbol,
        "approval_id": snapshot.approval_id,
        "entry_side": snapshot.entry_side,
        "action": snapshot.plan.action,
        "summary": snapshot.plan.summary,
        "rationale": snapshot.plan.rationale,
        "market_price": _decimal_text(getattr(snapshot.signal, "last_close", "0")),
        "entry_price": _decimal_text(snapshot.active_position.get("entry_price") or "0"),
        "unrealized_pnl_usd": _decimal_text(snapshot.plan.unrealized_pnl_usd),
        "unrealized_pnl_pct": _decimal_text(snapshot.plan.pnl_pct),
        "current_stop_price": (
            _decimal_text(snapshot.protective_orders.get("stop_loss_price"))
            if snapshot.protective_orders.get("stop_loss")
            else ""
        ),
        "current_take_profit_price": (
            _decimal_text(snapshot.protective_orders.get("take_profit_price"))
            if snapshot.protective_orders.get("take_profit")
            else ""
        ),
        "recommended_stop_price": _decimal_text(snapshot.recommended_stop_price),
        "recommended_take_profit_price": _decimal_text(snapshot.recommended_take_profit_price),
        "protective_orders_missing": snapshot.protective_orders_missing,
        "higher_timeframe_support": snapshot.plan.higher_timeframe_support,
        "higher_timeframe_total": snapshot.plan.higher_timeframe_total,
        "position_side": str(snapshot.active_position.get("side") or "LONG").strip().upper(),
    }


def _build_live_adjustment_whatsapp_message(result: dict[str, Any]) -> str:
    management = result.get("management") or {}
    premium_assessment = result.get("premium_assessment") or {}
    symbol = str(management.get("symbol") or result.get("symbol") or "").strip().upper() or "n/d"
    approval_id = str(management.get("approval_id") or "").strip().upper() or "sin approval id"
    symbol_shortcut = _symbol_shortcut(symbol)
    lines = [
        f"Ajuste live {symbol} | {approval_id}",
        (
            f"Mercado {_format_price_text(management.get('market_price') or '0')} | "
            f"PnL {_format_usd_text(management.get('unrealized_pnl_usd') or '0')} USD "
            f"({_format_pct_text(management.get('unrealized_pnl_pct') or '0')}%)"
        ),
        (
            f"SL {_display_price_text(management.get('current_stop_price'))} -> {_display_price_text(management.get('recommended_stop_price'))} | "
            f"TP {_display_price_text(management.get('current_take_profit_price'))} -> {_display_price_text(management.get('recommended_take_profit_price'))}"
        ),
        f"Plan: {management.get('summary') or 'ajuste ejecutado'}",
        "Seguimiento: esperar radar 15m",
    ]
    if str(premium_assessment.get("risk_label") or "").strip().lower() == "alto_riesgo":
        lines.insert(4, "Etiqueta premium: ALTO RIESGO")
    return "\n".join(lines)


def _build_live_adjustment_status_whatsapp_message(result: dict[str, Any]) -> str:
    management = result.get("management") or {}
    symbol = str(management.get("symbol") or result.get("symbol") or "").strip().upper() or "n/d"
    approval_id = str(management.get("approval_id") or "").strip().upper()
    symbol_shortcut = _symbol_shortcut(symbol)
    header = result.get("error") or "No hay ajuste live accionable en este momento."
    if approval_id:
        header = f"{header}\n{symbol} | {approval_id}"
    else:
        header = f"{header}\n{symbol}"
    lines = [header]
    if management:
        lines.append(
            (
                f"Mercado {_format_price_text(management.get('market_price') or '0')} | "
                f"SL {_display_price_text(management.get('current_stop_price'))} | "
                f"TP {_display_price_text(management.get('current_take_profit_price'))}"
            )
        )
        lines.append(f"Plan: {management.get('summary') or 'sin cambios'}")
    follow_up = []
    if symbol_shortcut:
        follow_up.append("esperar radar 15m")
        if result.get("reason_code") == "no_change":
            follow_up.append(f"AJUSTAR {symbol_shortcut} solo cuando Hermes lo pida")
    if follow_up:
        lines.append("Seguimiento: " + " | ".join(follow_up))
    return "\n".join(lines)


def _build_paper_close_whatsapp_message(closed: dict[str, Any]) -> str:
    position = closed.get("position") or {}
    lines = [
        f"Paper cerrado {position.get('symbol')} {position.get('side')} | {position.get('position_id')}",
        f"Salida: {_operator_timestamp(str(closed.get('closed_at', '') or ''))} a {_format_price_text(closed.get('exit_price') or '0')} | Trigger {_trigger_label(str(closed.get('trigger', '') or ''))}",
        f"PnL {_format_usd_text(closed.get('realized_pnl_usd') or '0')} USD ({_format_pct_text(closed.get('realized_pnl_pct') or '0')}%) | Duracion {closed.get('duration_human') or 'n/d'}",
        f"Motivo: {str(closed.get('reason', '') or '').strip() or 'n/d'}",
    ]
    follow_up = _format_follow_up_line(closed.get("commands") or {}, include_close=False)
    if follow_up:
        lines.append(follow_up)
    return "\n".join(lines)


def _build_paper_status_whatsapp_message(status: dict[str, Any]) -> str:
    normalized_status = str(status.get("status", "") or "").strip().lower()
    if normalized_status == "closed":
        return _build_paper_close_whatsapp_message(status)

    if normalized_status == "open":
        position = status.get("position") or {}
        risk = status.get("risk") or {}
        commands = status.get("commands") or {}
        lines = [
            f"Paper activo {position.get('symbol')} {position.get('side')} | {position.get('position_id')} | {position.get('approval_id') or 'sin approval id'}",
            f"Entrada: {_operator_timestamp(str(status.get('opened_at', '') or position.get('opened_at', '') or ''))} a {_format_price_text(position.get('entry_price') or '0')}",
        ]
        pnl_line = f"PnL flotante {_format_usd_text(status.get('unrealized_pnl_usd') or '0')} USD"
        unrealized_pct = status.get("unrealized_pnl_pct")
        if unrealized_pct is not None:
            pnl_line += f" ({_format_pct_text(unrealized_pct)}%)"
        market_price = status.get("market_price")
        if market_price is not None:
            pnl_line = f"Mercado {_format_price_text(market_price)} | {pnl_line}"
        pnl_line += f" | Duracion {status.get('duration_human') or 'n/d'}"
        lines.append(pnl_line)
        lines.append(
            f"Notional {_format_usd_text(risk.get('notional_usd') or position.get('notional_usd') or '0')} USD | Riesgo max {_format_usd_text(risk.get('estimated_max_loss_usd') or '0')} USD | R/B {risk.get('risk_reward_ratio') or 'n/d'}"
        )
        lines.append(
            f"SL {_format_price_text(risk.get('stop_loss_price') or position.get('stop_loss_price') or '0')} | TP {_format_price_text(risk.get('take_profit_price') or position.get('take_profit_price') or '0')}"
        )
        follow_up = _format_follow_up_line(commands)
        if follow_up:
            lines.append(follow_up)
        return "\n".join(lines)

    if normalized_status.startswith("approval_"):
        approval = status.get("approval") or {}
        proposal = approval.get("proposal") or {}
        commands = status.get("commands") or {}
        approval_id = str(approval.get("approval_id") or "").strip() or "n/d"
        approval_state = str(approval.get("status") or normalized_status.removeprefix("approval_") or "unknown").strip().lower()
        label = {
            "pending": "pendiente",
            "approved": "aprobada",
            "denied": "rechazada",
            "expired": "expirada",
        }.get(approval_state, approval_state or "desconocida")
        lines = [
            f"Aprobacion {approval_id} {label}",
            (
                f"{str(proposal.get('side', '') or '').strip().upper()} "
                f"{str(proposal.get('symbol', '') or '').strip().upper()} | "
                f"Notional {_format_usd_text(proposal.get('notional_usd') or '0')} USD | "
                f"SL {proposal.get('stop_loss_pct') or '0'}% | TP {proposal.get('take_profit_pct') or '0'}%"
            ).strip(),
        ]
        created_at = _operator_timestamp(str(approval.get("created_at", "") or ""))
        expires_at = _operator_timestamp(str(approval.get("expires_at", "") or ""))
        if created_at != "n/d" or expires_at != "n/d":
            lines.append(f"Creada {created_at} | Expira {expires_at}")
        market_summary = str(approval.get("market_summary", "") or "").strip()
        if market_summary:
            lines.append(f"Tesis: {market_summary}")
        follow_up_parts: list[str] = []
        status_trade = str(commands.get("status_trade", "") or "").strip()
        status_symbol = str(commands.get("status_symbol", "") or "").strip()
        approve_trade = str(commands.get("approve_trade", "") or "").strip()
        approve_symbol = str(commands.get("approve_symbol", "") or "").strip()
        reject_trade = str(commands.get("reject_trade", "") or "").strip()
        reject_symbol = str(commands.get("reject_symbol", "") or "").strip()
        if status_trade:
            follow_up_parts.append(status_trade)
        if status_symbol and status_symbol not in follow_up_parts:
            follow_up_parts.append(status_symbol)
        if approval_state == "pending" and approve_trade:
            follow_up_parts.append(approve_trade)
        if approval_state == "pending" and approve_symbol and approve_symbol not in follow_up_parts:
            follow_up_parts.append(approve_symbol)
        if approval_state == "pending" and reject_trade:
            follow_up_parts.append(reject_trade)
        if approval_state == "pending" and reject_symbol and reject_symbol not in follow_up_parts:
            follow_up_parts.append(reject_symbol)
        if follow_up_parts:
            lines.append("Seguimiento: " + " | ".join(follow_up_parts))
        return "\n".join(lines)

    reference = str((status.get("commands") or {}).get("status_trade") or (status.get("commands") or {}).get("status_position") or "").replace("ESTADO ", "").strip()
    if reference:
        return f"Estado paper {reference}: {normalized_status or 'desconocido'}."
    return f"Estado paper: {normalized_status or 'desconocido'}."


def _build_paper_daily_summary_whatsapp_message(summary: dict[str, Any]) -> str:
    open_positions = summary.get("open_positions") or []
    doge_scorecard = summary.get("doge_strategy_scorecard") or {}
    lines = [
        f"Resumen paper {summary.get('date') or 'n/d'}",
        (
            f"Entradas {summary.get('entries_count', 0)} | Salidas {summary.get('exits_count', 0)} | "
            f"PnL realizado {_format_usd_text(summary.get('realized_pnl_usd') or '0')} USD"
        ),
        (
            f"Aprobaciones pedidas {summary.get('approvals_requested', 0)} | "
            f"Aprobadas {summary.get('approvals_approved', 0)} | Rechazadas {summary.get('approvals_denied', 0)}"
        ),
        f"Posiciones abiertas {summary.get('open_positions_count', 0)}",
    ]
    if open_positions:
        preview = ", ".join(
            f"{str(position.get('symbol', '') or '').strip().upper()} {str(position.get('side', '') or '').strip().upper()}"
            for position in open_positions[:3]
        )
        if preview:
            suffix = "..." if len(open_positions) > 3 else ""
            lines.append(f"Abiertas: {preview}{suffix}")
    if isinstance(doge_scorecard, dict) and int(doge_scorecard.get("total_matches", 0) or 0) > 0:
        top_pair = None
        for candidate in list(doge_scorecard.get("strategy_regime_pairs") or []):
            if isinstance(candidate, dict):
                top_pair = candidate
                break
        preview_parts = [
            (
                f"DOGE conv {doge_scorecard.get('approval_conversion_pct', '0')}% | "
                f"expectancy {_format_usd_text(doge_scorecard.get('expectancy_usd') or '0')} USD | "
                f"hold med {str(doge_scorecard.get('median_hold_human', '') or 'n/d').strip() or 'n/d'}"
            )
        ]
        if top_pair is not None:
            preview_parts.append(
                (
                    f"top {str(top_pair.get('strategy_id', '') or 'unknown').strip()} x "
                    f"{str(top_pair.get('regime_label', '') or 'unknown').strip()}"
                )
            )
        lines.append("Scorecard: " + " | ".join(preview_parts))
    return "\n".join(lines)


def _paper_position_status_result(
    *,
    reference_id: str = "",
    position_id: str = "",
    approval_id: str = "",
    include_market_price: bool = True,
) -> dict[str, Any]:
    normalized_reference = str(reference_id or "").strip().upper()
    normalized_position_id = str(position_id or "").strip().upper()
    normalized_approval_id = str(approval_id or "").strip().upper()
    if normalized_reference and not normalized_position_id and not normalized_approval_id:
        if normalized_reference.startswith("PPOS-"):
            normalized_position_id = normalized_reference
        elif normalized_reference.startswith("TRADE-"):
            normalized_approval_id = normalized_reference

    status = get_paper_position_status(
        reference_id=normalized_reference,
        position_id=normalized_position_id,
        approval_id=normalized_approval_id,
    )
    if status.get("success") and status.get("status") == "open" and include_market_price:
        symbol = str((status.get("position") or {}).get("symbol", "") or "").strip().upper()
        if symbol:
            price_result = _price_result(symbol)
            if price_result.get("success"):
                return get_paper_position_status(
                    reference_id=normalized_reference,
                    position_id=normalized_position_id,
                    approval_id=normalized_approval_id,
                    reference_price=Decimal(str(price_result["reference_price"])),
                )
            status["market_price_error"] = price_result.get("error")
    if status.get("success"):
        return status

    approval_key = normalized_approval_id or (normalized_reference if normalized_reference.startswith("TRADE-") else "")
    if approval_key:
        approval = get_trade_approval(approval_key)
        if approval is not None:
            approval_status = str(approval.get("status", "") or "").strip().lower() or "unknown"
            return {
                "success": True,
                "status": f"approval_{approval_status}",
                "approval": approval,
                "commands": {
                    "status_trade": f"ESTADO {approval_key}",
                    "approve_trade": f"APROBAR {approval_key}",
                    "reject_trade": f"RECHAZAR {approval_key}",
                },
            }
    return status


def _send_whatsapp_home_message(message: str) -> dict[str, Any]:
    _ensure_runtime_env_loaded()
    target = os.getenv("WHATSAPP_HOME_CHANNEL", "").strip()
    if not target:
        return {"success": False, "error": "WHATSAPP_HOME_CHANNEL is not configured"}
    if not _parse_bool(os.getenv("BINANCE_NOTIFY_WHATSAPP"), default=True):
        return {"success": False, "skipped": True, "reason": "BINANCE_NOTIFY_WHATSAPP is false"}
    payload = json.dumps({"chatId": target, "message": message}).encode("utf-8")
    request = urllib.request.Request(
        "http://127.0.0.1:3000/send",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read().decode("utf-8")
    except Exception as exc:
        return {"success": False, "chat_id": target, "error": str(exc)}
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {"raw": raw}
    return {"success": True, "chat_id": target, "response": body}


def _paper_account_result(symbol: str = "") -> dict[str, Any]:
    return get_paper_account_overview(symbol=symbol)


def _resolve_paper_account_snapshot(
    *,
    symbol: str,
    free_balance_usd: float,
    open_positions: int,
    positions_in_symbol: int,
    daily_realized_pnl_usd: float,
    use_persistent_account: bool,
) -> tuple[BinanceAccountSnapshot, Optional[dict[str, Any]], str]:
    if use_persistent_account:
        overview = _paper_account_result(symbol)
        snapshot = BinanceAccountSnapshot.from_payload(overview.get("account_snapshot") or {})
        return snapshot, overview, "paper_state"
    snapshot = BinanceAccountSnapshot.from_payload(
        {
            "free_balance_usd": free_balance_usd,
            "open_positions": open_positions,
            "positions_in_symbol": positions_in_symbol,
            "daily_realized_pnl_usd": daily_realized_pnl_usd,
            "kill_switch_active": is_kill_switch_active(),
        }
    )
    return snapshot, None, "payload"


def _account_snapshot_result(symbol: str = "") -> dict[str, Any]:
    try:
        executor = _get_live_executor(require_credentials=True)
        overview = executor.fetch_account_overview(symbol=symbol or None)
    except BinanceLiveExecutionError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "execution_mode": "live-readonly",
        **overview,
    }


def _adjust_live_trade_protection_result(
    *,
    symbol: str,
    timeframe: str = "15m",
    score_threshold: int = 5,
    context_timeframes: tuple[str, ...] = ("1h", "4h"),
    default_stop_loss_pct: Decimal = Decimal("0.5"),
    default_take_profit_pct: Decimal = Decimal("1.0"),
    notify_whatsapp: bool = False,
) -> dict[str, Any]:
    _ensure_runtime_env_loaded()
    limits = BinanceRiskLimits.from_env()
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return {"success": False, "error": "symbol is required", "reason_code": "invalid_symbol"}
    if limits.mode != "live" or not limits.live_trading_enabled:
        return {
            "success": False,
            "error": "Live no esta armado para ajustes reales.",
            "reason_code": "live_disabled",
            "symbol": normalized_symbol,
        }
    if limits.allowed_symbols and normalized_symbol not in limits.allowed_symbols:
        return {
            "success": False,
            "error": f"{normalized_symbol} no esta permitido por la politica activa.",
            "reason_code": "symbol_not_allowed",
            "symbol": normalized_symbol,
        }

    try:
        executor = _get_live_executor(require_credentials=True)
        snapshot = build_doge_live_management_snapshot(
            executor,
            symbol=normalized_symbol,
            timeframe=timeframe,
            score_threshold=score_threshold,
            context_timeframes=context_timeframes,
            default_stop_loss_pct=default_stop_loss_pct,
            default_take_profit_pct=default_take_profit_pct,
        )
    except BinanceLiveExecutionError as exc:
        return {"success": False, "error": str(exc), "reason_code": "exchange_error", "symbol": normalized_symbol}
    except Exception as exc:
        return {"success": False, "error": str(exc), "reason_code": "management_error", "symbol": normalized_symbol}

    if snapshot is None:
        return {
            "success": False,
            "error": f"No hay una posicion live abierta en {normalized_symbol} para ajustar.",
            "reason_code": "no_position",
            "symbol": normalized_symbol,
        }

    management = _management_result_payload(snapshot)
    material_payload = build_doge_adjustment_premium_payload(snapshot, timeframe=timeframe)
    current_fingerprint = material_fingerprint(material_payload)
    premium_request = get_latest_doge_premium_analysis_request(
        symbol=normalized_symbol,
        request_kind="adjustment",
    )
    if premium_request is not None and str(premium_request.get("event_fingerprint") or "").strip().upper() == current_fingerprint:
        premium_status = str(premium_request.get("status") or "").strip().lower()
        premium_outcome = str(premium_request.get("analysis_outcome") or "").strip().lower()
        if premium_status == "pending":
            return {
                "success": False,
                "error": "Hay un analisis premium pendiente para este ajuste DOGE.",
                "reason_code": "premium_pending",
                "symbol": normalized_symbol,
                "management": management,
                "premium_request": premium_request,
            }
        if premium_status == "completed" and premium_outcome == "rejected":
            return {
                "success": False,
                "error": "Gemini 3.5 Flash descarto este ajuste para el evento actual.",
                "reason_code": "premium_rejected",
                "symbol": normalized_symbol,
                "management": management,
                "premium_request": premium_request,
            }

    effective_stop_price = snapshot.recommended_stop_price
    effective_take_profit_price = snapshot.recommended_take_profit_price
    premium_assessment = {}
    if premium_request is not None and str(premium_request.get("analysis_outcome") or "").strip().lower() == "passed":
        premium_assessment = premium_request.get("analysis") or {}
        rules = executor._get_symbol_rules(normalized_symbol)
        suggested_stop_price = str(premium_assessment.get("suggested_stop_price") or "").strip()
        suggested_take_profit_price = str(premium_assessment.get("suggested_take_profit_price") or "").strip()
        if suggested_stop_price:
            try:
                effective_stop_price = executor.normalize_protective_price(
                    symbol=normalized_symbol,
                    entry_side=snapshot.entry_side,
                    purpose="stop_loss",
                    price=Decimal(suggested_stop_price),
                    rules=rules,
                )
            except Exception:
                effective_stop_price = snapshot.recommended_stop_price
        if suggested_take_profit_price:
            try:
                effective_take_profit_price = executor.normalize_protective_price(
                    symbol=normalized_symbol,
                    entry_side=snapshot.entry_side,
                    purpose="take_profit",
                    price=Decimal(suggested_take_profit_price),
                    rules=rules,
                )
            except Exception:
                effective_take_profit_price = snapshot.recommended_take_profit_price
        management["recommended_stop_price"] = _decimal_text(effective_stop_price)
        management["recommended_take_profit_price"] = _decimal_text(effective_take_profit_price)

    if snapshot.plan.action == "exit_defensive":
        return {
            "success": False,
            "error": "La lectura actual pide revisar salida defensiva, no mover SL/TP.",
            "reason_code": "defensive_exit",
            "symbol": normalized_symbol,
            "management": management,
        }
    if not snapshot.actionable_adjustment:
        return {
            "success": False,
            "error": "DOGE no necesita ajuste live ahora; la proteccion ya esta alineada.",
            "reason_code": "no_change",
            "symbol": normalized_symbol,
            "management": management,
        }

    try:
        adjustment = executor.adjust_protective_orders(
            normalized_symbol,
            entry_side=snapshot.entry_side,
            stop_loss_price=effective_stop_price,
            take_profit_price=effective_take_profit_price,
            current_orders=dict(snapshot.protective_orders),
        )
    except BinanceLiveExecutionError as exc:
        return {
            "success": False,
            "error": str(exc),
            "reason_code": "adjustment_failed",
            "symbol": normalized_symbol,
            "management": management,
        }

    result = {
        "success": True,
        "symbol": normalized_symbol,
        "execution_mode": "live-adjustment",
        "management": management,
        "adjustment": adjustment,
    }
    approval_payload = getattr(snapshot, "approval", {}) or {}
    result["adjustment_event"] = record_live_trade_protection_adjustment(
        symbol=normalized_symbol,
        approval_id=str(getattr(snapshot, "approval_id", "") or approval_payload.get("approval_id") or ""),
        management=management,
        adjustment=adjustment,
        premium_request=premium_request if premium_request is not None else None,
        premium_assessment=premium_assessment or None,
        decision_context=approval_payload.get("decision_context") or None,
    )
    if premium_request is not None and str(premium_request.get("analysis_outcome") or "").strip().lower() == "passed":
        result["premium_request"] = premium_request
        result["premium_assessment"] = premium_assessment
    if notify_whatsapp:
        result["whatsapp_notification"] = _send_whatsapp_home_message(
            _build_live_adjustment_whatsapp_message(result)
        )
    return result


def _paper_decision_result(
    *,
    symbol: str,
    side: str,
    notional_usd: float,
    mode: str,
    order_type: str,
    stop_loss_pct: float,
    take_profit_pct: float,
    leverage: float,
    free_balance_usd: float,
    open_positions: int,
    positions_in_symbol: int,
    daily_realized_pnl_usd: float,
    verifier_model: str,
    verifier_passed: bool,
    verifier_confidence: float,
    rationale: str,
    macro_alignment: str,
    dry_run: bool,
    use_persistent_account: bool,
) -> dict[str, Any]:
    resolved_mode, limits, active_limits = _resolve_effective_risk_limits(mode)
    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": symbol,
            "side": side,
            "notional_usd": notional_usd,
            "mode": resolved_mode,
            "order_type": order_type,
            "stop_loss_pct": stop_loss_pct or None,
            "take_profit_pct": take_profit_pct or None,
            "leverage": leverage,
            "verifier_model": verifier_model,
            "verifier_passed": verifier_passed,
            "verifier_confidence": verifier_confidence or None,
            "rationale": rationale,
            "macro_alignment": macro_alignment,
            "dry_run": dry_run,
        }
    )
    account, paper_account_overview, account_source = _resolve_paper_account_snapshot(
        symbol=symbol,
        free_balance_usd=free_balance_usd,
        open_positions=open_positions,
        positions_in_symbol=positions_in_symbol,
        daily_realized_pnl_usd=daily_realized_pnl_usd,
        use_persistent_account=use_persistent_account,
    )
    decision = evaluate_trade_proposal(
        proposal,
        account,
        limits,
        kill_switch_active=is_kill_switch_active(),
    )
    return {
        "proposal": proposal,
        "account": account,
        "decision": decision,
        "account_source": account_source,
        "paper_account": paper_account_overview,
        "active_risk_mode": active_limits.mode,
        "effective_risk_mode": limits.mode,
    }


def _submit_trade_result(
    *,
    symbol: str,
    side: str,
    notional_usd: float,
    mode: str,
    order_type: str,
    stop_loss_pct: float,
    take_profit_pct: float,
    leverage: float,
    free_balance_usd: float,
    open_positions: int,
    positions_in_symbol: int,
    daily_realized_pnl_usd: float,
    verifier_model: str,
    verifier_passed: bool,
    verifier_confidence: float,
    rationale: str,
    dry_run: bool,
    macro_alignment: str = "aligned",
    approval_id: str = "",
    evidence_id: str = "",
    use_persistent_account: bool = True,
    notify_whatsapp: bool = False,
) -> dict[str, Any]:
    normalized_mode, _, _ = _resolve_effective_risk_limits(mode)
    requested_live = normalized_mode == "live"
    limits = BinanceRiskLimits.from_env()

    if not requested_live:
        paper = _paper_decision_result(
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            mode=mode,
            order_type=order_type,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            leverage=leverage,
            free_balance_usd=free_balance_usd,
            open_positions=open_positions,
            positions_in_symbol=positions_in_symbol,
            daily_realized_pnl_usd=daily_realized_pnl_usd,
            verifier_model=verifier_model,
            verifier_passed=verifier_passed,
            verifier_confidence=verifier_confidence,
            rationale=rationale,
            macro_alignment=macro_alignment,
            dry_run=dry_run,
            use_persistent_account=use_persistent_account,
        )
        decision = paper["decision"]
        if not decision.allowed:
            return {
                "success": False,
                "error": "trade rejected by risk guardrails",
                "decision": decision.to_dict(),
                "account_source": paper.get("account_source"),
                "paper_account": paper.get("paper_account"),
            }
        if str(mode).strip().lower() == "paper" and not dry_run:
            if _parse_bool(os.getenv("BINANCE_REQUIRE_TRADE_APPROVAL"), default=True):
                approved, approval_error, approval_record = validate_trade_approval(
                    approval_id,
                    paper["proposal"],
                )
                if not approved:
                    return {
                        "success": False,
                        "error": approval_error,
                        "decision": decision.to_dict(),
                        "paper_account": paper.get("paper_account"),
                    }
            else:
                approval_record = None

            price_result = _price_result(symbol)
            if not price_result.get("success"):
                return {
                    "success": False,
                    "error": price_result.get("error") or "could not resolve a reference price for paper execution",
                    "decision": decision.to_dict(),
                    "paper_account": paper.get("paper_account"),
                }

            execution = open_paper_position(
                paper["proposal"],
                reference_price=_get_live_executor(require_credentials=False).get_reference_price(symbol),
                approval_id=approval_id,
                evidence_id=evidence_id,
                decision_context=(approval_record or {}).get("decision_context"),
            )
            if approval_record is not None:
                consume_trade_approval(approval_id, paper["proposal"])

            whatsapp_notification = None
            if notify_whatsapp:
                whatsapp_notification = _send_whatsapp_home_message(
                    _build_paper_entry_whatsapp_message(execution)
                )
            return {
                "success": True,
                "execution_mode": "paper",
                "decision": decision.to_dict(),
                "paper_account": execution["account_snapshot"],
                "paper_position": execution["position"],
                "paper_execution": execution,
                "approval": approval_record,
                "whatsapp_notification": whatsapp_notification,
            }
        return {
            "success": True,
            "execution_mode": "dry_run",
            "decision": decision.to_dict(),
            "account_source": paper.get("account_source"),
            "paper_account": paper.get("paper_account"),
            "order_preview": {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "mode": normalized_mode,
                "notional_usd": notional_usd,
                "rationale": rationale,
            },
        }

    try:
        executor = _get_live_executor(require_credentials=True)
        overview = executor.fetch_account_overview(symbol=symbol)
    except BinanceLiveExecutionError as exc:
        return {"success": False, "error": str(exc)}

    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": symbol,
            "side": side,
            "notional_usd": notional_usd,
            "mode": normalized_mode,
            "order_type": order_type,
            "stop_loss_pct": stop_loss_pct or None,
            "take_profit_pct": take_profit_pct or None,
            "leverage": leverage,
            "verifier_model": verifier_model,
            "verifier_passed": verifier_passed,
            "verifier_confidence": verifier_confidence or None,
            "rationale": rationale,
            "macro_alignment": macro_alignment,
            "dry_run": dry_run,
        }
    )
    account = BinanceAccountSnapshot.from_payload(overview.get("account_snapshot") or {})
    decision = evaluate_trade_proposal(
        proposal,
        account,
        limits,
        kill_switch_active=is_kill_switch_active(),
    )
    if not decision.allowed:
        return {
            "success": False,
            "error": "live trade rejected by risk guardrails after refreshing account snapshot",
            "decision": decision.to_dict(),
            "live_account_overview": overview,
        }

    try:
        exchange_order_preview = executor.preview_trade(proposal)
    except BinanceLiveExecutionError as exc:
        return {
            "success": False,
            "error": str(exc),
            "decision": decision.to_dict(),
            "live_account_overview": overview,
        }

    if dry_run:
        return {
            "success": True,
            "execution_mode": "dry_run",
            "decision": decision.to_dict(),
            "account_source": "live_exchange",
            "live_account_overview": overview,
            "exchange_order_preview": exchange_order_preview,
            "order_preview": {
                "symbol": symbol,
                "side": side,
                "order_type": order_type,
                "mode": normalized_mode,
                "notional_usd": notional_usd,
                "rationale": rationale,
            },
        }

    approval_record = None
    if _parse_bool(os.getenv("BINANCE_REQUIRE_TRADE_APPROVAL"), default=True):
        approved, approval_error, approval_record = validate_trade_approval(
            approval_id,
            proposal,
        )
        if not approved:
            return {
                "success": False,
                "error": approval_error,
                "decision": decision.to_dict(),
                "live_account_overview": overview,
            }

    try:
        execution = executor.submit_trade(proposal)
    except BinanceLiveExecutionError as exc:
        live_failure_event = record_live_trade_execution_failure(
            proposal=proposal,
            error=str(exc),
            approval_id=approval_id,
            stage="submit_trade",
            rollback_sent="emergency rollback sent successfully" in str(exc).lower(),
            details={
                "evidence_id": str(evidence_id or "").strip() or None,
                "mode": normalized_mode,
            },
            decision_context=(approval_record or {}).get("decision_context"),
        )
        return {
            "success": False,
            "error": str(exc),
            "decision": decision.to_dict(),
            "live_account_overview": overview,
            "live_execution_failure": live_failure_event,
        }

    if approval_record is not None:
        approval_record = consume_trade_approval(approval_id, proposal)

    live_execution_event = record_live_trade_execution_success(
        proposal=proposal,
        execution=execution,
        approval_id=approval_id,
        evidence_id=evidence_id,
        details={"mode": normalized_mode},
        decision_context=(approval_record or {}).get("decision_context"),
    )

    return {
        "success": True,
        "execution_mode": "live",
        "decision": decision.to_dict(),
        "live_account_overview": overview,
        "execution": execution,
        "approval": approval_record,
        "live_execution_event": live_execution_event,
    }


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(
            f"binance-guarded MCP server requires the 'mcp' package: {exc}"
        ) from exc

    mcp = FastMCP(
        "binance-guarded",
        instructions=(
            "Guarded Binance trading surface. Use it to validate candidate trades, "
            "inspect the active risk profile, and toggle a kill switch. This "
            "surface defaults to the active risk profile mode, but may route live futures "
            "orders only when explicit live credentials and risk mode are enabled."
        ),
    )

    @mcp.tool()
    def binance_seed_paper_account(starting_balance_usd: float = 1000.0, reset: bool = False) -> str:
        """Create or reset the persistent paper trading account used by semi-autonomous trading."""

        return json.dumps(
            seed_paper_account(
                starting_balance_usd=Decimal(str(starting_balance_usd)),
                reset=reset,
            ),
            indent=2,
        )

    @mcp.tool()
    def binance_paper_account(symbol: str = "") -> str:
        """Return the persistent paper account snapshot, reserve usage, and open positions."""

        return json.dumps(_paper_account_result(symbol), indent=2)

    @mcp.tool()
    def binance_paper_position_status(
        reference_id: str = "",
        position_id: str = "",
        approval_id: str = "",
        include_market_price: bool = True,
    ) -> str:
        """Return the current or final paper-position snapshot for a trade approval ID or paper position ID."""

        return json.dumps(
            _paper_position_status_result(
                reference_id=reference_id,
                position_id=position_id,
                approval_id=approval_id,
                include_market_price=include_market_price,
            ),
            indent=2,
        )

    @mcp.tool()
    def binance_risk_profile() -> str:
        """Return the active Binance risk limits and kill switch state."""

        _ensure_runtime_env_loaded()
        limits = BinanceRiskLimits.from_env()
        live_adapter_status = "not configured"
        live_adapter_configured = False
        try:
            _get_live_executor(require_credentials=True)
            live_adapter_status = "configured"
            live_adapter_configured = True
        except BinanceLiveExecutionError as exc:
            live_adapter_status = str(exc)
        return json.dumps(
            {
                "success": True,
                "risk_profile": limits.to_dict(),
                "kill_switch_active": is_kill_switch_active(),
                "kill_switch_path": str(get_kill_switch_path()),
                "execution_mode": "live-enabled" if limits.live_trading_enabled else "paper-first",
                "live_adapter_configured": live_adapter_configured,
                "live_adapter_status": live_adapter_status,
            },
            indent=2,
        )

    @mcp.tool()
    def binance_latest_price(symbol: str) -> str:
        """Return the current Binance futures reference price for a symbol."""

        return json.dumps(_price_result(symbol), indent=2)

    @mcp.tool()
    def binance_account_snapshot(symbol: str = "") -> str:
        """Return a live read-only Binance futures account snapshot."""

        return json.dumps(_account_snapshot_result(symbol), indent=2)

    @mcp.tool()
    def binance_set_kill_switch(enabled: bool, reason: str = "") -> str:
        """Toggle the local kill switch that blocks every trading action."""

        state = set_kill_switch(enabled=enabled, reason=reason)
        return json.dumps({"success": True, "kill_switch": state}, indent=2)

    @mcp.tool()
    def binance_validate_trade(
        symbol: str,
        side: str,
        notional_usd: float,
        mode: str = "auto",
        order_type: str = "MARKET",
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        leverage: float = 1.0,
        free_balance_usd: float = 0.0,
        open_positions: int = 0,
        positions_in_symbol: int = 0,
        daily_realized_pnl_usd: float = 0.0,
        verifier_model: str = "",
        verifier_passed: bool = False,
        verifier_confidence: float = 0.0,
        rationale: str = "",
        macro_alignment: str = "aligned",
        dry_run: bool = True,
        use_persistent_account: bool = True,
    ) -> str:
        """Validate a trade proposal against the active risk policy; use mode='paper' only for an explicit local simulation."""

        paper = _paper_decision_result(
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            mode=mode,
            order_type=order_type,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            leverage=leverage,
            free_balance_usd=free_balance_usd,
            open_positions=open_positions,
            positions_in_symbol=positions_in_symbol,
            daily_realized_pnl_usd=daily_realized_pnl_usd,
            verifier_model=verifier_model,
            verifier_passed=verifier_passed,
            verifier_confidence=verifier_confidence,
            rationale=rationale,
            macro_alignment=macro_alignment,
            dry_run=dry_run,
            use_persistent_account=use_persistent_account,
        )
        return json.dumps(
            {
                "success": True,
                "decision": paper["decision"].to_dict(),
                "account_source": paper.get("account_source"),
                "paper_account": paper.get("paper_account"),
                "active_risk_mode": paper.get("active_risk_mode"),
                "effective_risk_mode": paper.get("effective_risk_mode"),
            },
            indent=2,
        )

    @mcp.tool()
    def binance_record_market_evidence(
        symbol: str,
        timeframe: str,
        market_summary: str,
        binance_reference_price: float,
        external_reference_price: float,
        source_urls: str,
        external_source_name: str = "",
        momentum_summary: str = "",
    ) -> str:
        """Persist a multi-source market evidence packet before requesting a trade approval."""

        try:
            evidence = record_market_evidence(
                symbol=symbol,
                timeframe=timeframe,
                market_summary=market_summary,
                binance_reference_price=Decimal(str(binance_reference_price)),
                external_reference_price=Decimal(str(external_reference_price)),
                source_urls=source_urls,
                external_source_name=external_source_name,
                momentum_summary=momentum_summary,
            )
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2)
        return json.dumps({"success": True, "evidence": evidence}, indent=2)

    @mcp.tool()
    def binance_request_trade_approval(
        symbol: str,
        side: str,
        notional_usd: float,
        mode: str = "auto",
        order_type: str = "MARKET",
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        leverage: float = 1.0,
        free_balance_usd: float = 0.0,
        open_positions: int = 0,
        positions_in_symbol: int = 0,
        daily_realized_pnl_usd: float = 0.0,
        verifier_model: str = "",
        verifier_passed: bool = False,
        verifier_confidence: float = 0.0,
        rationale: str = "",
        macro_alignment: str = "aligned",
        dry_run: bool = False,
        evidence_id: str = "",
        market_summary: str = "",
        expires_minutes: int = 0,
        use_persistent_account: bool = True,
        notify_whatsapp: bool = False,
    ) -> str:
        """Create a formal trade approval request that follows the active risk mode unless mode='paper' is requested explicitly."""

        paper = _paper_decision_result(
            symbol=symbol,
            side=side,
            notional_usd=notional_usd,
            mode=mode,
            order_type=order_type,
            stop_loss_pct=stop_loss_pct,
            take_profit_pct=take_profit_pct,
            leverage=leverage,
            free_balance_usd=free_balance_usd,
            open_positions=open_positions,
            positions_in_symbol=positions_in_symbol,
            daily_realized_pnl_usd=daily_realized_pnl_usd,
            verifier_model=verifier_model,
            verifier_passed=verifier_passed,
            verifier_confidence=verifier_confidence,
            rationale=rationale,
            macro_alignment=macro_alignment,
            dry_run=dry_run,
            use_persistent_account=use_persistent_account,
        )
        decision = paper["decision"]
        if not decision.allowed:
            return json.dumps(
                {
                    "success": False,
                    "error": "trade rejected by risk guardrails",
                    "decision": decision.to_dict(),
                    "paper_account": paper.get("paper_account"),
                },
                indent=2,
            )
        try:
            approval = request_trade_approval(
                paper["proposal"],
                evidence_id=evidence_id,
                market_summary=market_summary,
                expires_minutes=expires_minutes,
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "success": False,
                    "error": str(exc),
                    "decision": decision.to_dict(),
                },
                indent=2,
            )

        whatsapp_notification = None
        if notify_whatsapp:
            whatsapp_notification = _send_whatsapp_home_message(
                _build_paper_approval_whatsapp_message(
                    approval_id=approval["approval_id"],
                    symbol=symbol,
                    side=side,
                    notional_usd=notional_usd,
                    stop_loss_pct=stop_loss_pct,
                    take_profit_pct=take_profit_pct,
                    expires_at=str(approval.get("expires_at") or ""),
                    symbol_shortcut=str(symbol).upper().removesuffix("USDT"),
                )
            )
        return json.dumps(
            {
                "success": True,
                "approval": approval,
                "decision": decision.to_dict(),
                "paper_account": paper.get("paper_account"),
                "active_risk_mode": paper.get("active_risk_mode"),
                "effective_risk_mode": paper.get("effective_risk_mode"),
                "whatsapp_notification": whatsapp_notification,
            },
            indent=2,
        )

    @mcp.tool()
    def binance_record_trade_approval(
        approval_id: str,
        decision: str,
        response_text: str = "",
        responder: str = "operator",
        notify_whatsapp: bool = False,
    ) -> str:
        """Record the operator's WhatsApp approval or denial for a pending trade approval ID."""

        try:
            approval = record_trade_approval(
                approval_id,
                decision=decision,
                response_text=response_text,
                responder=responder,
            )
        except ValueError as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2)

        whatsapp_notification = None
        if notify_whatsapp:
            label = "aprobada" if approval.get("status") == "approved" else "rechazada"
            whatsapp_notification = _send_whatsapp_home_message(
                f"Aprobacion {approval.get('approval_id')} {label}. Estado actual: {approval.get('status')}.\n"
                f"Seguimiento: ESTADO {approval.get('approval_id')}"
            )
        return json.dumps(
            {
                "success": True,
                "approval": approval,
                "whatsapp_notification": whatsapp_notification,
            },
            indent=2,
        )

    @mcp.tool()
    def binance_submit_trade(
        symbol: str,
        side: str,
        notional_usd: float,
        mode: str = "auto",
        order_type: str = "MARKET",
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        leverage: float = 1.0,
        free_balance_usd: float = 0.0,
        open_positions: int = 0,
        positions_in_symbol: int = 0,
        daily_realized_pnl_usd: float = 0.0,
        verifier_model: str = "",
        verifier_passed: bool = False,
        verifier_confidence: float = 0.0,
        rationale: str = "",
        macro_alignment: str = "aligned",
        dry_run: bool = True,
        approval_id: str = "",
        evidence_id: str = "",
        use_persistent_account: bool = True,
        notify_whatsapp: bool = False,
    ) -> str:
        """Submit a guarded preview or trade using the active risk mode unless mode='paper' is requested explicitly."""

        return json.dumps(
            _submit_trade_result(
                symbol=symbol,
                side=side,
                notional_usd=notional_usd,
                mode=mode,
                order_type=order_type,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                leverage=leverage,
                free_balance_usd=free_balance_usd,
                open_positions=open_positions,
                positions_in_symbol=positions_in_symbol,
                daily_realized_pnl_usd=daily_realized_pnl_usd,
                verifier_model=verifier_model,
                verifier_passed=verifier_passed,
                verifier_confidence=verifier_confidence,
                rationale=rationale,
                macro_alignment=macro_alignment,
                dry_run=dry_run,
                approval_id=approval_id,
                evidence_id=evidence_id,
                use_persistent_account=use_persistent_account,
                notify_whatsapp=notify_whatsapp,
            ),
            indent=2,
        )

    @mcp.tool()
    def binance_close_paper_position(
        position_id: str,
        reason: str = "manual thesis invalidation",
        notify_whatsapp: bool = False,
    ) -> str:
        """Close an open paper position at the latest Binance reference price and journal the result."""

        position = get_open_paper_position(position_id)
        if position is None:
            return json.dumps({"success": False, "error": f"position_id '{position_id}' was not found"}, indent=2)
        price_result = _price_result(position.get("symbol", ""))
        if not price_result.get("success"):
            return json.dumps({"success": False, "error": price_result.get("error")}, indent=2)
        closed = close_paper_position(
            position_id,
            exit_price=Decimal(str(price_result["reference_price"])),
            reason=reason,
            trigger="manual",
        )
        whatsapp_notification = None
        if notify_whatsapp:
            whatsapp_notification = _send_whatsapp_home_message(
                _build_paper_close_whatsapp_message(closed)
            )
        return json.dumps(
            {
                "success": True,
                "closed_position": closed,
                "whatsapp_notification": whatsapp_notification,
            },
            indent=2,
        )

    @mcp.tool()
    def binance_reconcile_paper_positions(symbol: str = "", notify_whatsapp: bool = True) -> str:
        """Check every open paper position against its stop loss / take profit and auto-close triggered exits."""

        def _lookup(symbol_name: str) -> Decimal:
            result = _price_result(symbol_name)
            if not result.get("success"):
                raise BinanceLiveExecutionError(result.get("error") or f"price lookup failed for {symbol_name}")
            return Decimal(str(result["reference_price"]))

        try:
            reconciled = reconcile_protective_exits(_lookup, symbol=symbol)
        except (BinanceLiveExecutionError, ValueError) as exc:
            return json.dumps({"success": False, "error": str(exc)}, indent=2)

        notifications: list[dict[str, Any]] = []
        if notify_whatsapp:
            for closed in reconciled.get("closed_positions", []):
                notifications.append(
                    _send_whatsapp_home_message(_build_paper_close_whatsapp_message(closed))
                )

        return json.dumps(
            {
                "success": True,
                "reconciled": reconciled,
                "whatsapp_notifications": notifications,
            },
            indent=2,
        )


    @mcp.tool()
    def binance_execute_arbitrage(symbol: str, capital_usd: float, market_price: float, funding_rate: float, dry_run: bool = False, leverage: int = 2, notify_whatsapp: bool = True) -> str:
        """Executes a Phase 2 delta-neutral arbitrage (Spots & Futures). Use when Scout advises an Arbitrage."""
        _ensure_runtime_env_loaded()
        try:
            plan = plan_delta_neutral_arbitrage(
                symbol=symbol,
                available_capital_usd=Decimal(str(capital_usd)),
                market_price=Decimal(str(market_price)),
                funding_rate=Decimal(str(funding_rate)),
                leverage=Decimal(str(leverage))
            )
            result = execute_arbitrage(plan, dry_run=dry_run)
            
            if notify_whatsapp and result.get("success"):
                if dry_run:
                    msg = (
                        "🧪 SIMULACION FASE 2 ARBITRAJE EXITOSA\nPar: " + symbol +
                        "\nSpot Buy: " + str(result.get("spot_buy_qty")) + " DOGE" +
                        "\nTransf: " + str(result.get("transfer_amount")) + " USDT" +
                        "\nFutures Sell: " + str(result.get("futures_short_qty")) + " DOGE"
                    )
                else:
                    msg = (
                        "✅ ENTRAMOS A ARBITRAJE DELTA NEUTRAL\nPar: " + symbol +
                        "\nExecution ID: " + str(result.get("execution_id")) +
                        "\nYield Esperado > 0.10%\nAcciones:\n- Spot (Leg 1) COMPRADO\n- Universal Transfer EFECTUADO\n- Futures (Leg 2) SHORTEADO"
                    )
                _send_whatsapp_home_message(msg)
            elif notify_whatsapp and not result.get("success"):
                _send_whatsapp_home_message("FAIL: FALLO FASE 2 ARBITRAJE " + symbol + "\nError: " + str(result.get('error')))
                
            return json.dumps(result, indent=2)
        except Exception as e:
            msg = "FAIL: ERROR INTERNO FASE 2 ARBITRAJE " + symbol + ": " + str(e)
            if notify_whatsapp: _send_whatsapp_home_message(msg)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    @mcp.tool()
    def binance_execute_grid(symbol: str, capital_usd: float, market_price: float, atr: float, dry_run: bool = False, grids_per_side: int = 3, trend_bias_pct: float = 0.0, notify_whatsapp: bool = True) -> str:
        """Executes a Phase 3 dynamic ATR-based grid. Use when Scout advises a Grid."""
        _ensure_runtime_env_loaded()
        try:
            plan = plan_dynamic_grid(
                symbol=symbol,
                market_price=Decimal(str(market_price)),
                atr=Decimal(str(atr)),
                available_capital=Decimal(str(capital_usd)),
                grids_per_side=grids_per_side,
                atr_multiplier=Decimal("1.5"),
                trend_bias_pct=Decimal(str(trend_bias_pct)),
                leverage=get_strategy_leverage_cap("grid"),
            )
            result = execute_grid(plan, dry_run=dry_run)
            
            if notify_whatsapp and result.get("success"):
                if dry_run:
                    msg = "🧪 SIMULACION FASE 3 GRID EXITOSA\nPar: " + symbol + "\nOrdenes Calculadas: " + str(len(plan.levels))
                else:
                    msg = "✅ GRID DINAMICA DESPLEGADA\nPar: " + symbol + "\nOrdenes Generadas: " + str(result.get('orders_placed')) + "\nCapital Asignado: " + str(capital_usd) + " USD\nATR Registrado: " + str(atr)
                _send_whatsapp_home_message(msg)
            elif notify_whatsapp and not result.get("success"):
                _send_whatsapp_home_message("FAIL: FALLO FASE 3 GRID " + symbol + "\nError: " + str(result.get('error')))

            return json.dumps(result, indent=2)
        except Exception as e:
            msg = "FAIL: ERROR INTERNO FASE 3 GRID " + symbol + ": " + str(e)
            if notify_whatsapp: _send_whatsapp_home_message(msg)
            return json.dumps({"success": False, "error": str(e)}, indent=2)

    @mcp.tool()
    def binance_reconcile_grid(symbol: str = "", notify_whatsapp: bool = True) -> str:
        """Monitor active live grids and freeze them if price breaks outside their configured range."""
        _ensure_runtime_env_loaded()
        try:
            result = reconcile_grid(symbol=symbol)
            notifications: list[dict[str, Any]] = []
            if notify_whatsapp:
                for item in result.get("reconciled", []):
                    residual = item.get("residual_position") or {}
                    residual_text = "sin inventario residual"
                    if residual:
                        residual_text = (
                            f"inventario residual {residual.get('position_amt', '0')} @ {residual.get('entry_price', '0')}"
                        )
                    notifications.append(
                        _send_whatsapp_home_message(
                            "GRID DOGE BLOQUEADA POR BREAKOUT\n"
                            + "Par: " + str(item.get("symbol") or "")
                            + "\nExecution ID: " + str(item.get("execution_id") or "")
                            + "\nLado ruptura: " + str(item.get("breakout_side") or "")
                            + "\nPrecio referencia: " + str(item.get("reference_price") or "")
                            + "\nEstado: " + str(item.get("status") or "")
                            + "\nResidual: " + residual_text
                        )
                    )
            return json.dumps({"success": True, "reconciled": result, "whatsapp_notifications": notifications}, indent=2)
        except Exception as exc:
            if notify_whatsapp:
                _send_whatsapp_home_message("FAIL: ERROR GRID RECONCILER " + str(symbol or "DOGEUSDT") + "\nError: " + str(exc))
            return json.dumps({"success": False, "error": str(exc)}, indent=2)

    return mcp




def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"binance-guarded MCP server cannot start: {exc}\n")
        return 2

    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - integration surface
        logger.exception("binance-guarded MCP server crashed")
        sys.stderr.write(f"binance-guarded MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())