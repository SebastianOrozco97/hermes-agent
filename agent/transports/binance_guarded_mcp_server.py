"""Guarded Binance MCP server.

This server intentionally starts with a paper-first surface. Every trade
proposal is validated against the local risk policy before an execution
envelope is returned. Live order routing remains blocked until a dedicated
exchange adapter is added on top of this policy layer.
"""

from __future__ import annotations

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
from tools.binance_guardrails import (
    BinanceAccountSnapshot,
    BinanceRiskLimits,
    BinanceTradeProposal,
    _parse_bool,
    evaluate_trade_proposal,
    get_kill_switch_path,
    is_kill_switch_active,
    set_kill_switch,
)
from tools.binance_paper_runtime import (
    close_paper_position,
    consume_trade_approval,
    get_open_paper_position,
    get_paper_position_status,
    get_paper_account_overview,
    get_trade_approval,
    open_paper_position,
    record_market_evidence,
    record_trade_approval,
    reconcile_protective_exits,
    request_trade_approval,
    seed_paper_account,
    validate_trade_approval,
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
        override_existing = _LOADED_ENV_PATH == env_key and _LOADED_ENV_MTIME_NS is not None
        try:
            load_dotenv(str(env_path), override=override_existing, encoding="utf-8")
        except UnicodeDecodeError:
            load_dotenv(str(env_path), override=override_existing, encoding="latin-1")
    _LOADED_ENV_PATH = env_key
    _LOADED_ENV_MTIME_NS = env_mtime_ns


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
) -> str:
    risk = _proposal_risk_snapshot(notional_usd, stop_loss_pct, take_profit_pct)
    return "\n".join(
        (
            f"Aprobacion requerida {approval_id}: {side.upper()} {symbol.upper()}.",
            f"Notional {risk['notional_usd']} USD | Riesgo max {risk['estimated_max_loss_usd']} USD | R/B {risk.get('risk_reward_ratio') or 'n/d'}.",
            f"Responde APROBAR {approval_id} o RECHAZAR {approval_id}.",
            f"Seguimiento: ESTADO {approval_id}",
        )
    )


def _build_paper_entry_whatsapp_message(execution: dict[str, Any]) -> str:
    position = execution.get("position") or {}
    risk = execution.get("risk") or {}
    commands = execution.get("commands") or {}
    lines = [
        f"Paper ejecutado {position.get('side')} {position.get('symbol')} | {position.get('position_id')} | {position.get('approval_id') or 'sin approval id'}",
        f"Fill: {_operator_timestamp(str(execution.get('filled_at', '') or position.get('opened_at', '') or ''))} a {_decimal_text(execution.get('fill_reference_price') or position.get('entry_price') or '0')}",
        f"Notional {risk.get('notional_usd') or position.get('notional_usd') or '0'} USD | Riesgo max {risk.get('estimated_max_loss_usd') or 'n/d'} USD | R/B {risk.get('risk_reward_ratio') or 'n/d'}",
        f"SL {risk.get('stop_loss_price') or position.get('stop_loss_price') or 'n/d'} | TP {risk.get('take_profit_price') or position.get('take_profit_price') or 'n/d'}",
    ]
    follow_up = _format_follow_up_line(commands)
    if follow_up:
        lines.append(follow_up)
    return "\n".join(lines)


def _build_paper_close_whatsapp_message(closed: dict[str, Any]) -> str:
    position = closed.get("position") or {}
    lines = [
        f"Paper cerrado {position.get('symbol')} {position.get('side')} | {position.get('position_id')}",
        f"Salida: {_operator_timestamp(str(closed.get('closed_at', '') or ''))} a {_decimal_text(closed.get('exit_price') or '0')} | Trigger {_trigger_label(str(closed.get('trigger', '') or ''))}",
        f"PnL {closed.get('realized_pnl_usd') or '0'} USD ({closed.get('realized_pnl_pct') or 'n/d'}%) | Duracion {closed.get('duration_human') or 'n/d'}",
        f"Motivo: {str(closed.get('reason', '') or '').strip() or 'n/d'}",
    ]
    follow_up = _format_follow_up_line(closed.get("commands") or {}, include_close=False)
    if follow_up:
        lines.append(follow_up)
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
    dry_run: bool,
    use_persistent_account: bool,
) -> dict[str, Any]:
    _ensure_runtime_env_loaded()
    proposal = BinanceTradeProposal.from_payload(
        {
            "symbol": symbol,
            "side": side,
            "notional_usd": notional_usd,
            "mode": mode,
            "order_type": order_type,
            "stop_loss_pct": stop_loss_pct or None,
            "take_profit_pct": take_profit_pct or None,
            "leverage": leverage,
            "verifier_model": verifier_model,
            "verifier_passed": verifier_passed,
            "verifier_confidence": verifier_confidence or None,
            "rationale": rationale,
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
        BinanceRiskLimits.from_env(),
        kill_switch_active=is_kill_switch_active(),
    )
    return {
        "proposal": proposal,
        "account": account,
        "decision": decision,
        "account_source": account_source,
        "paper_account": paper_account_overview,
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
    approval_id: str = "",
    evidence_id: str = "",
    use_persistent_account: bool = True,
    notify_whatsapp: bool = True,
) -> dict[str, Any]:
    requested_live = str(mode).strip().lower() == "live" and not dry_run
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
                "mode": mode,
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
            "mode": mode,
            "order_type": order_type,
            "stop_loss_pct": stop_loss_pct or None,
            "take_profit_pct": take_profit_pct or None,
            "leverage": leverage,
            "verifier_model": verifier_model,
            "verifier_passed": verifier_passed,
            "verifier_confidence": verifier_confidence or None,
            "rationale": rationale,
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
        execution = executor.submit_trade(proposal)
    except BinanceLiveExecutionError as exc:
        return {
            "success": False,
            "error": str(exc),
            "decision": decision.to_dict(),
            "live_account_overview": overview,
        }

    return {
        "success": True,
        "execution_mode": "live",
        "decision": decision.to_dict(),
        "live_account_overview": overview,
        "execution": execution,
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
            "surface stays paper-first by default, but may route live futures "
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
        mode: str = "paper",
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
        dry_run: bool = True,
        use_persistent_account: bool = True,
    ) -> str:
        """Validate a trade proposal against the mandatory local risk policy."""

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
            dry_run=dry_run,
            use_persistent_account=use_persistent_account,
        )
        return json.dumps(
            {
                "success": True,
                "decision": paper["decision"].to_dict(),
                "account_source": paper.get("account_source"),
                "paper_account": paper.get("paper_account"),
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
        mode: str = "paper",
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
        dry_run: bool = False,
        evidence_id: str = "",
        market_summary: str = "",
        expires_minutes: int = 0,
        use_persistent_account: bool = True,
        notify_whatsapp: bool = True,
    ) -> str:
        """Create a formal trade approval request that the operator must approve from WhatsApp before entry."""

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
                )
            )
        return json.dumps(
            {
                "success": True,
                "approval": approval,
                "decision": decision.to_dict(),
                "paper_account": paper.get("paper_account"),
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
        notify_whatsapp: bool = True,
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
        mode: str = "paper",
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
        dry_run: bool = True,
        approval_id: str = "",
        evidence_id: str = "",
        use_persistent_account: bool = True,
        notify_whatsapp: bool = True,
    ) -> str:
        """Submit a guarded paper preview or a live trade when explicitly armed."""

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
        notify_whatsapp: bool = True,
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