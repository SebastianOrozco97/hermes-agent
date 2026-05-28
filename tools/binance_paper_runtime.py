from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlparse

import hashlib
import json
import os
import uuid

from hermes_constants import get_hermes_home
from tools.binance_guardrails import BinanceAccountSnapshot, BinanceTradeProposal


_DEFAULT_PAPER_BALANCE = Decimal("1000")
_MULTI_SOURCE_SYMBOLS = {"BTCUSDT", "ETHUSDT"}


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now_utc().isoformat()


def _parse_iso_datetime(value: str) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        if text.endswith("Z"):
            try:
                return datetime.fromisoformat(text[:-1] + "+00:00")
            except ValueError:
                return None
        return None


def _format_duration_seconds(total_seconds: int) -> str:
    remaining = max(0, int(total_seconds))
    days, remaining = divmod(remaining, 86400)
    hours, remaining = divmod(remaining, 3600)
    minutes, seconds = divmod(remaining, 60)
    parts: list[str] = []
    if days:
        parts.append(f"{days}d")
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{seconds}s")
    return " ".join(parts)


def _duration_snapshot(opened_at: str, closed_at: str = "") -> dict[str, Any]:
    opened = _parse_iso_datetime(opened_at)
    ended = _parse_iso_datetime(closed_at) if closed_at else _now_utc()
    if opened is None or ended is None:
        return {"duration_seconds": None, "duration_human": None}
    if opened.tzinfo is None:
        opened = opened.replace(tzinfo=timezone.utc)
    if ended.tzinfo is None:
        ended = ended.replace(tzinfo=timezone.utc)
    duration_seconds = max(0, int((ended - opened).total_seconds()))
    return {
        "duration_seconds": duration_seconds,
        "duration_human": _format_duration_seconds(duration_seconds),
    }


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal value") from exc


def _parse_int(value: Any, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _decimal_to_str(value: Decimal) -> str:
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f")


def _split_source_urls(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = [
            part.strip()
            for part in str(value).replace("\r", "\n").replace(",", "\n").split("\n")
        ]
    items: list[str] = []
    for item in raw_items:
        if item and item not in items:
            items.append(item)
    return tuple(items)


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlparse(value)
    except ValueError:
        return False
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _has_external_confirmation_url(urls: Sequence[str]) -> bool:
    for value in urls:
        try:
            hostname = (urlparse(value).hostname or "").lower()
        except ValueError:
            continue
        if hostname and hostname != "binance.com" and not hostname.endswith(".binance.com"):
            return True
    return False


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return default
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")


def _normalize_json_payload(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _decimal_to_str(value)
    if isinstance(value, Mapping):
        return {str(key): _normalize_json_payload(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_normalize_json_payload(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _normalize_decision_context(value: Any) -> Optional[dict[str, Any]]:
    if value in (None, ""):
        return None
    normalized = _normalize_json_payload(value)
    if not isinstance(normalized, dict):
        raise ValueError("decision_context must be a mapping when provided")
    return normalized


def _iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    except OSError:
        return []
    return records


def _starting_balance_from_env() -> Decimal:
    raw = os.getenv("BINANCE_PAPER_STARTING_BALANCE_USD", "1000").strip() or "1000"
    return _parse_decimal(raw, "BINANCE_PAPER_STARTING_BALANCE_USD")


def _trade_approval_ttl_minutes(default: int = 30) -> int:
    raw = os.getenv("BINANCE_TRADE_APPROVAL_TTL_MIN", str(default)).strip() or str(default)
    return max(1, _parse_int(raw, "BINANCE_TRADE_APPROVAL_TTL_MIN"))


def _doge_premium_analysis_ttl_minutes(default: int = 5) -> int:
    raw = os.getenv("DOGE_PREMIUM_ANALYSIS_TTL_MIN", str(default)).strip() or str(default)
    return max(1, _parse_int(raw, "DOGE_PREMIUM_ANALYSIS_TTL_MIN"))


def get_paper_state_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "binance-paper-state.json"


def get_paper_journal_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "binance-paper-journal.jsonl"


def get_market_evidence_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "binance-market-evidence.jsonl"


def get_trade_approvals_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "binance-trade-approvals.json"


def get_doge_premium_requests_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "doge-premium-analysis-requests.json"


def _default_state(starting_balance: Decimal) -> dict[str, Any]:
    return {
        "version": 1,
        "cash_balance_usd": _decimal_to_str(starting_balance),
        "open_positions": [],
        "last_reset_at": _now_iso(),
    }


def _load_state(home: Optional[Path] = None) -> dict[str, Any]:
    state = _read_json(get_paper_state_path(home=home), None)
    if not isinstance(state, dict):
        return _default_state(_starting_balance_from_env())
    if "cash_balance_usd" not in state:
        return _default_state(_starting_balance_from_env())
    state.setdefault("open_positions", [])
    state.setdefault("version", 1)
    state.setdefault("last_reset_at", _now_iso())
    return state


def _save_state(state: dict[str, Any], home: Optional[Path] = None) -> None:
    _write_json(get_paper_state_path(home=home), state)


def _load_approvals(home: Optional[Path] = None) -> dict[str, Any]:
    payload = _read_json(get_trade_approvals_path(home=home), None)
    if not isinstance(payload, dict):
        return {"version": 1, "approvals": []}
    payload.setdefault("version", 1)
    payload.setdefault("approvals", [])
    return payload


def _save_approvals(payload: dict[str, Any], home: Optional[Path] = None) -> None:
    _write_json(get_trade_approvals_path(home=home), payload)


def _load_doge_premium_requests(home: Optional[Path] = None) -> dict[str, Any]:
    payload = _read_json(get_doge_premium_requests_path(home=home), None)
    if not isinstance(payload, dict):
        return {"version": 1, "requests": []}
    payload.setdefault("version", 1)
    payload.setdefault("requests", [])
    return payload


def _save_doge_premium_requests(payload: dict[str, Any], home: Optional[Path] = None) -> None:
    _write_json(get_doge_premium_requests_path(home=home), payload)


@dataclass(frozen=True)
class PaperPosition:
    position_id: str
    symbol: str
    side: str
    entry_price: Decimal
    quantity: Decimal
    notional_usd: Decimal
    leverage: Decimal
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    stop_loss_price: Decimal
    take_profit_price: Decimal
    rationale: str
    approval_id: Optional[str]
    evidence_id: Optional[str]
    opened_at: str
    decision_context: Optional[dict[str, Any]] = None

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PaperPosition":
        return cls(
            position_id=str(payload.get("position_id", "")).strip(),
            symbol=str(payload.get("symbol", "")).strip().upper(),
            side=str(payload.get("side", "")).strip().upper(),
            entry_price=_parse_decimal(payload.get("entry_price", "0"), "entry_price"),
            quantity=_parse_decimal(payload.get("quantity", "0"), "quantity"),
            notional_usd=_parse_decimal(payload.get("notional_usd", "0"), "notional_usd"),
            leverage=_parse_decimal(payload.get("leverage", "1"), "leverage"),
            stop_loss_pct=_parse_decimal(payload.get("stop_loss_pct", "0"), "stop_loss_pct"),
            take_profit_pct=_parse_decimal(payload.get("take_profit_pct", "0"), "take_profit_pct"),
            stop_loss_price=_parse_decimal(payload.get("stop_loss_price", "0"), "stop_loss_price"),
            take_profit_price=_parse_decimal(payload.get("take_profit_price", "0"), "take_profit_price"),
            rationale=str(payload.get("rationale", "") or "").strip(),
            approval_id=str(payload.get("approval_id", "") or "").strip() or None,
            evidence_id=str(payload.get("evidence_id", "") or "").strip() or None,
            opened_at=str(payload.get("opened_at", "") or "").strip() or _now_iso(),
            decision_context=_normalize_decision_context(payload.get("decision_context")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "position_id": self.position_id,
            "symbol": self.symbol,
            "side": self.side,
            "entry_price": _decimal_to_str(self.entry_price),
            "quantity": _decimal_to_str(self.quantity),
            "notional_usd": _decimal_to_str(self.notional_usd),
            "leverage": _decimal_to_str(self.leverage),
            "stop_loss_pct": _decimal_to_str(self.stop_loss_pct),
            "take_profit_pct": _decimal_to_str(self.take_profit_pct),
            "stop_loss_price": _decimal_to_str(self.stop_loss_price),
            "take_profit_price": _decimal_to_str(self.take_profit_price),
            "rationale": self.rationale,
            "approval_id": self.approval_id,
            "evidence_id": self.evidence_id,
            "opened_at": self.opened_at,
            "decision_context": self.decision_context,
        }


def _position_risk_summary(position: PaperPosition) -> dict[str, Any]:
    estimated_max_loss_usd = (position.notional_usd * position.stop_loss_pct) / Decimal("100")
    estimated_max_profit_usd = (position.notional_usd * position.take_profit_pct) / Decimal("100")
    risk_reward_ratio: Optional[str] = None
    if estimated_max_loss_usd > 0 and estimated_max_profit_usd > 0:
        risk_reward_ratio = _decimal_to_str(estimated_max_profit_usd / estimated_max_loss_usd)
    return {
        "notional_usd": _decimal_to_str(position.notional_usd),
        "stop_loss_pct": _decimal_to_str(position.stop_loss_pct),
        "take_profit_pct": _decimal_to_str(position.take_profit_pct),
        "stop_loss_price": _decimal_to_str(position.stop_loss_price),
        "take_profit_price": _decimal_to_str(position.take_profit_price),
        "estimated_max_loss_usd": _decimal_to_str(estimated_max_loss_usd),
        "estimated_max_profit_usd": _decimal_to_str(estimated_max_profit_usd),
        "risk_reward_ratio": risk_reward_ratio,
    }


def _follow_up_commands(position: PaperPosition) -> dict[str, str]:
    commands = {
        "status_position": f"ESTADO {position.position_id}",
        "close_position": f"CERRAR {position.position_id}",
    }
    if position.approval_id:
        commands["status_trade"] = f"ESTADO {position.approval_id}"
    return commands


def _pnl_pct_from_notional(*, pnl_usd: Decimal, notional_usd: Decimal) -> Optional[str]:
    if notional_usd <= 0:
        return None
    return _decimal_to_str((pnl_usd / notional_usd) * Decimal("100"))


def _trigger_category(trigger: str, reason: str = "") -> str:
    normalized_trigger = str(trigger or "").strip().lower()
    normalized_reason = str(reason or "").strip().lower()
    if normalized_trigger in {"stop_loss", "take_profit"}:
        return "protective_exit"
    if "breakout" in normalized_trigger or "breakout" in normalized_reason:
        return "breakout_exit"
    if normalized_trigger in {"manual", "close_command", "operator"}:
        return "manual_exit"
    return normalized_trigger or "unknown"


def _derive_exit_attribution(
    *,
    position: PaperPosition,
    realized_pnl_usd: Decimal,
    trigger: str,
    reason: str,
    risk: Mapping[str, Any],
    duration: Mapping[str, Any],
    thesis_outcome: str = "",
    failure_mode: str = "",
) -> dict[str, Any]:
    normalized_trigger = str(trigger or "manual").strip().lower() or "manual"
    normalized_reason = str(reason or "").strip().lower()
    trigger_category = _trigger_category(normalized_trigger, normalized_reason)

    resolved_thesis_outcome = str(thesis_outcome or "").strip().lower()
    if not resolved_thesis_outcome:
        if normalized_trigger == "take_profit" and realized_pnl_usd > 0:
            resolved_thesis_outcome = "validated"
        elif normalized_trigger == "stop_loss" and realized_pnl_usd < 0:
            resolved_thesis_outcome = "invalidated"
        elif trigger_category == "breakout_exit":
            resolved_thesis_outcome = "regime_shift_exit"
        elif realized_pnl_usd > 0:
            resolved_thesis_outcome = "managed_profit"
        elif realized_pnl_usd < 0:
            resolved_thesis_outcome = "managed_loss"
        else:
            resolved_thesis_outcome = "flat_exit"

    resolved_failure_mode = str(failure_mode or "").strip().lower()
    if not resolved_failure_mode:
        if any(keyword in normalized_reason for keyword in ("execution", "slippage", "latency")):
            resolved_failure_mode = "execution_degradation"
        elif normalized_trigger == "stop_loss":
            resolved_failure_mode = "thesis_failure"
        elif trigger_category == "breakout_exit":
            resolved_failure_mode = "regime_breakout"
        elif trigger_category == "manual_exit" and realized_pnl_usd < 0:
            resolved_failure_mode = "operator_override"
        elif normalized_trigger == "take_profit":
            resolved_failure_mode = "none"
        else:
            resolved_failure_mode = "managed_exit"

    thesis_outcome_tags: list[str] = []
    if resolved_thesis_outcome == "validated":
        thesis_outcome_tags.extend(["thesis_worked", "target_hit"])
    elif resolved_thesis_outcome == "invalidated":
        thesis_outcome_tags.extend(["thesis_failed", "stop_loss_hit"])
    elif resolved_thesis_outcome == "regime_shift_exit":
        thesis_outcome_tags.extend(["regime_shift", "breakout_exit"])
    elif resolved_thesis_outcome == "managed_profit":
        thesis_outcome_tags.extend(["managed_exit", "profit_locked"])
    elif resolved_thesis_outcome == "managed_loss":
        thesis_outcome_tags.extend(["managed_exit", "loss_capped"])
    else:
        thesis_outcome_tags.append(resolved_thesis_outcome)

    if trigger_category not in thesis_outcome_tags:
        thesis_outcome_tags.append(trigger_category)
    if resolved_failure_mode == "execution_degradation" and "execution_degraded" not in thesis_outcome_tags:
        thesis_outcome_tags.append("execution_degraded")

    attribution_bucket = "strategy"
    if resolved_failure_mode == "execution_degradation":
        attribution_bucket = "execution"
    elif trigger_category in {"protective_exit", "manual_exit", "breakout_exit"}:
        attribution_bucket = "management"

    selected_strategy = dict(position.decision_context.get("selected_strategy") or {}) if position.decision_context else {}
    expected_vs_realized = {
        "selected_strategy_id": str(selected_strategy.get("strategy_id") or "").strip() or None,
        "expected_edge": selected_strategy.get("expected_edge"),
        "expected_holding_horizon": selected_strategy.get("holding_horizon"),
        "expected_max_profit_usd": risk.get("estimated_max_profit_usd"),
        "expected_max_loss_usd": risk.get("estimated_max_loss_usd"),
        "realized_pnl_usd": _decimal_to_str(realized_pnl_usd),
        "realized_pnl_pct": _pnl_pct_from_notional(
            pnl_usd=realized_pnl_usd,
            notional_usd=position.notional_usd,
        ),
        "realized_duration_seconds": duration.get("duration_seconds"),
        "realized_duration_human": duration.get("duration_human"),
        "exit_trigger": normalized_trigger,
        "trigger_category": trigger_category,
    }

    return {
        "trigger_category": trigger_category,
        "thesis_outcome": resolved_thesis_outcome,
        "thesis_outcome_tags": thesis_outcome_tags,
        "failure_mode": resolved_failure_mode,
        "attribution_bucket": attribution_bucket,
        "expected_vs_realized": expected_vs_realized,
    }


def _position_status_payload(
    position: PaperPosition,
    *,
    status: str,
    market_price: Optional[Decimal] = None,
    exit_price: Optional[Decimal] = None,
    realized_pnl_usd: Optional[Decimal] = None,
    closed_at: str = "",
    trigger: str = "",
    reason: str = "",
    trigger_category: str = "",
    thesis_outcome: str = "",
    thesis_outcome_tags: Optional[Sequence[str]] = None,
    failure_mode: str = "",
    attribution_bucket: str = "",
    expected_vs_realized: Optional[Mapping[str, Any]] = None,
    management_context: Optional[Mapping[str, Any]] = None,
    execution_context: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": status,
        "position": position.to_dict(),
        "opened_at": position.opened_at,
        "risk": _position_risk_summary(position),
        "commands": _follow_up_commands(position),
        **_duration_snapshot(position.opened_at, closed_at),
    }
    if market_price is not None:
        pnl_per_unit = market_price - position.entry_price
        if position.side == "SELL":
            pnl_per_unit = position.entry_price - market_price
        unrealized_pnl_usd = pnl_per_unit * position.quantity
        payload.update(
            {
                "market_price": _decimal_to_str(market_price),
                "unrealized_pnl_usd": _decimal_to_str(unrealized_pnl_usd),
                "unrealized_pnl_pct": _pnl_pct_from_notional(
                    pnl_usd=unrealized_pnl_usd,
                    notional_usd=position.notional_usd,
                ),
            }
        )
    if exit_price is not None:
        payload["exit_price"] = _decimal_to_str(exit_price)
    if realized_pnl_usd is not None:
        payload["realized_pnl_usd"] = _decimal_to_str(realized_pnl_usd)
        payload["realized_pnl_pct"] = _pnl_pct_from_notional(
            pnl_usd=realized_pnl_usd,
            notional_usd=position.notional_usd,
        )
    if closed_at:
        payload["closed_at"] = closed_at
    if trigger:
        payload["trigger"] = trigger
    if reason:
        payload["reason"] = reason
    if trigger_category:
        payload["trigger_category"] = trigger_category
    if thesis_outcome:
        payload["thesis_outcome"] = thesis_outcome
    if thesis_outcome_tags:
        payload["thesis_outcome_tags"] = list(thesis_outcome_tags)
    if failure_mode:
        payload["failure_mode"] = failure_mode
    if attribution_bucket:
        payload["attribution_bucket"] = attribution_bucket
    if expected_vs_realized:
        payload["expected_vs_realized"] = dict(expected_vs_realized)
    if management_context:
        payload["management_context"] = dict(management_context)
    if execution_context:
        payload["execution_context"] = dict(execution_context)
    return payload


def _reserved_margin_usd(open_positions: Sequence[dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for payload in open_positions:
        total += _parse_decimal(payload.get("notional_usd", "0"), "notional_usd")
    return total


def _daily_realized_pnl_usd(home: Optional[Path] = None) -> Decimal:
    today = _now_utc().date().isoformat()
    total = Decimal("0")
    for event in _iter_jsonl(get_paper_journal_path(home=home)):
        if event.get("event_type") != "paper_position_closed":
            continue
        if not str(event.get("closed_at", "")).startswith(today):
            continue
        total += _parse_decimal(event.get("realized_pnl_usd", "0"), "realized_pnl_usd")
    return total


def _normalize_history_label(value: Any) -> str:
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


def _unique_history_labels(values: Sequence[Any]) -> list[str]:
    labels: list[str] = []
    for raw_value in values:
        label = _normalize_history_label(raw_value)
        if label and label not in labels:
            labels.append(label)
    return labels


def _parse_history_boundary(value: str, *, end: bool = False) -> Optional[datetime]:
    text = str(value or "").strip()
    if not text:
        return None
    parsed = _parse_iso_datetime(text)
    if parsed is None:
        suffix = "T23:59:59.999999+00:00" if end else "T00:00:00+00:00"
        parsed = _parse_iso_datetime(text + suffix)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if "T" not in text and " " not in text:
        if end:
            parsed = parsed.replace(hour=23, minute=59, second=59, microsecond=999999)
        else:
            parsed = parsed.replace(hour=0, minute=0, second=0, microsecond=0)
    return parsed


def _history_event_timestamp(event: Mapping[str, Any]) -> Optional[datetime]:
    for key in ("closed_at", "opened_at", "recorded_at", "created_at"):
        parsed = _parse_iso_datetime(str(event.get(key, "") or "").strip())
        if parsed is None:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    return None


def _macro_direction_label(value: str) -> str:
    normalized = _normalize_history_label(value)
    if not normalized:
        return ""
    if "bear" in normalized:
        return "bearish"
    if "bull" in normalized:
        return "bullish"
    return normalized


def _derive_strategy_regime_labels(decision_context: Mapping[str, Any]) -> tuple[list[str], str]:
    selected_strategy = decision_context.get("selected_strategy") or {}
    if not isinstance(selected_strategy, Mapping):
        selected_strategy = {}
    market_context = decision_context.get("market_context") or {}
    if not isinstance(market_context, Mapping):
        market_context = {}
    macro_state = decision_context.get("macro_state") or {}
    if not isinstance(macro_state, Mapping):
        macro_state = {}

    strategy_regime_tags = _unique_history_labels(selected_strategy.get("regime_tags") or ())
    market_regime_tags = _unique_history_labels(market_context.get("regime_tags") or ())
    regime_labels = list(strategy_regime_tags)
    for label in market_regime_tags:
        if label not in regime_labels:
            regime_labels.append(label)

    macro_alignment = _normalize_history_label(selected_strategy.get("macro_alignment"))
    if macro_alignment:
        label = f"macro_{macro_alignment}"
        if label not in regime_labels:
            regime_labels.append(label)

    risk_level = _normalize_history_label(macro_state.get("risk_level"))
    if risk_level:
        label = f"risk_{risk_level}"
        if label not in regime_labels:
            regime_labels.append(label)

    trend_1h = _normalize_history_label(macro_state.get("btc_trend_1h"))
    trend_4h = _normalize_history_label(macro_state.get("btc_trend_4h"))
    if trend_1h:
        label = f"btc_1h_{trend_1h}"
        if label not in regime_labels:
            regime_labels.append(label)
    if trend_4h:
        label = f"btc_4h_{trend_4h}"
        if label not in regime_labels:
            regime_labels.append(label)

    macro_direction_1h = _macro_direction_label(trend_1h)
    macro_direction_4h = _macro_direction_label(trend_4h)
    if macro_direction_1h and macro_direction_1h == macro_direction_4h:
        macro_direction_label = f"{macro_direction_1h}_macro"
        if macro_direction_label not in regime_labels:
            regime_labels.append(macro_direction_label)
    elif macro_direction_1h and macro_direction_4h and macro_direction_1h != macro_direction_4h:
        if "mixed_macro" not in regime_labels:
            regime_labels.append("mixed_macro")

    primary_regime = "unknown"
    preferred_strategy_tags = [
        label
        for label in strategy_regime_tags
        if label not in {"directional_overlay", "range_capture", "delta_neutral"}
        and not label.endswith("m")
    ]
    if preferred_strategy_tags:
        primary_regime = preferred_strategy_tags[0]
    elif strategy_regime_tags:
        primary_regime = strategy_regime_tags[0]
    elif "bearish_macro" in regime_labels:
        primary_regime = "bearish_macro"
    elif "bullish_macro" in regime_labels:
        primary_regime = "bullish_macro"
    elif macro_alignment:
        primary_regime = f"macro_{macro_alignment}"
    elif market_regime_tags:
        primary_regime = market_regime_tags[0]

    return regime_labels, primary_regime


def _strategy_history_record(event: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    if event.get("event_type") != "paper_position_closed":
        return None
    timestamp = _history_event_timestamp(event)
    if timestamp is None:
        return None

    raw_decision_context = event.get("decision_context") or {}
    if not isinstance(raw_decision_context, Mapping):
        raw_decision_context = {}
    decision_context = _normalize_decision_context(raw_decision_context) or {}
    selected_strategy = decision_context.get("selected_strategy") or {}
    if not isinstance(selected_strategy, Mapping):
        selected_strategy = {}

    strategy_id = str(
        selected_strategy.get("strategy_id")
        or decision_context.get("selected_strategy_id")
        or "unknown"
    ).strip().lower() or "unknown"
    regime_labels, primary_regime = _derive_strategy_regime_labels(decision_context)
    thesis_outcome = str(event.get("thesis_outcome", "") or "").strip().lower()
    outcome_labels = _unique_history_labels([thesis_outcome, *(event.get("thesis_outcome_tags") or ())])

    return {
        "position_id": str(event.get("position_id", "") or "").strip(),
        "symbol": str(event.get("symbol", "") or "").strip().upper(),
        "strategy_id": strategy_id,
        "primary_regime": primary_regime,
        "regime_labels": regime_labels,
        "macro_alignment": str(selected_strategy.get("macro_alignment", "") or "").strip().lower(),
        "holding_horizon": str(selected_strategy.get("holding_horizon", "") or "").strip(),
        "expected_edge": str(selected_strategy.get("expected_edge", "") or "").strip(),
        "selection_confidence": str(selected_strategy.get("confidence", "") or "").strip(),
        "selector_family": str(decision_context.get("selector_family", "") or "").strip(),
        "opened_at": str(event.get("opened_at", "") or "").strip(),
        "closed_at": str(event.get("closed_at", "") or "").strip(),
        "realized_pnl_usd": str(event.get("realized_pnl_usd", "0") or "0").strip(),
        "realized_pnl_pct": str(event.get("realized_pnl_pct", "") or "").strip(),
        "trigger": str(event.get("trigger", "") or "").strip(),
        "trigger_category": str(event.get("trigger_category", "") or "").strip(),
        "thesis_outcome": thesis_outcome,
        "thesis_outcome_tags": list(event.get("thesis_outcome_tags") or ()),
        "failure_mode": str(event.get("failure_mode", "") or "").strip(),
        "attribution_bucket": str(event.get("attribution_bucket", "") or "").strip(),
        "expected_vs_realized": dict(event.get("expected_vs_realized") or {}),
        "decision_context": dict(decision_context),
        "_event_timestamp": timestamp,
        "_normalized_strategy_id": _normalize_history_label(strategy_id),
        "_normalized_regime_labels": _unique_history_labels(regime_labels),
        "_normalized_outcome_labels": outcome_labels,
    }


def get_paper_strategy_history(
    *,
    symbol: str = "",
    strategy_id: str = "",
    regime: str = "",
    outcome: str = "",
    start_date: str = "",
    end_date: str = "",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_strategy_id = _normalize_history_label(strategy_id)
    normalized_regime = _normalize_history_label(regime)
    normalized_outcome = _normalize_history_label(outcome)
    start_boundary = _parse_history_boundary(start_date, end=False)
    end_boundary = _parse_history_boundary(end_date, end=True)

    records: list[dict[str, Any]] = []
    total_realized_pnl = Decimal("0")
    for event in _iter_jsonl(get_paper_journal_path(home=home)):
        record = _strategy_history_record(event)
        if record is None:
            continue
        if normalized_symbol and record["symbol"] != normalized_symbol:
            continue
        if normalized_strategy_id and record["_normalized_strategy_id"] != normalized_strategy_id:
            continue
        if normalized_regime and normalized_regime not in record["_normalized_regime_labels"]:
            continue
        if normalized_outcome and normalized_outcome not in record["_normalized_outcome_labels"]:
            continue

        timestamp = record["_event_timestamp"]
        if start_boundary is not None and timestamp < start_boundary:
            continue
        if end_boundary is not None and timestamp > end_boundary:
            continue

        total_realized_pnl += _parse_decimal(record["realized_pnl_usd"], "realized_pnl_usd")
        public_record = {key: value for key, value in record.items() if not key.startswith("_")}
        records.append(public_record)

    records.sort(key=lambda item: str(item.get("closed_at", "") or ""), reverse=True)
    return {
        "success": True,
        "filters": {
            "symbol": normalized_symbol,
            "strategy_id": normalized_strategy_id,
            "regime": normalized_regime,
            "outcome": normalized_outcome,
            "start_date": str(start_date or "").strip(),
            "end_date": str(end_date or "").strip(),
        },
        "total_matches": len(records),
        "total_realized_pnl_usd": _decimal_to_str(total_realized_pnl),
        "records": records,
    }


def _update_history_bucket(bucket: dict[str, Any], record: Mapping[str, Any]) -> None:
    pnl = _parse_decimal(record.get("realized_pnl_usd", "0"), "realized_pnl_usd")
    bucket["closed_positions"] += 1
    bucket["realized_pnl_usd"] += pnl
    if pnl > 0:
        bucket["wins"] += 1
    elif pnl < 0:
        bucket["losses"] += 1
    else:
        bucket["flat"] += 1
    outcome = str(record.get("thesis_outcome", "") or "").strip().lower() or "unknown"
    bucket["outcomes"][outcome] = bucket["outcomes"].get(outcome, 0) + 1


def _finalize_history_bucket(
    key_name: str,
    key_value: str,
    bucket: Mapping[str, Any],
    *,
    regimes: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    closed_positions = int(bucket.get("closed_positions", 0) or 0)
    wins = int(bucket.get("wins", 0) or 0)
    realized_pnl_usd = Decimal(bucket.get("realized_pnl_usd", Decimal("0")))
    avg_realized_pnl = realized_pnl_usd / Decimal(closed_positions) if closed_positions else Decimal("0")
    win_rate_pct = Decimal("0")
    if closed_positions:
        win_rate_pct = (Decimal(wins) / Decimal(closed_positions)) * Decimal("100")
    finalized = {
        key_name: key_value,
        "closed_positions": closed_positions,
        "wins": wins,
        "losses": int(bucket.get("losses", 0) or 0),
        "flat": int(bucket.get("flat", 0) or 0),
        "win_rate_pct": _decimal_to_str(win_rate_pct),
        "realized_pnl_usd": _decimal_to_str(realized_pnl_usd),
        "avg_realized_pnl_usd": _decimal_to_str(avg_realized_pnl),
        "outcomes": dict(bucket.get("outcomes", {})),
    }
    if regimes is not None:
        finalized["regimes"] = regimes
    return finalized


def _history_bucket_sort_key(item: Mapping[str, Any], key_name: str) -> tuple[int, Decimal, str]:
    return (
        -int(item.get("closed_positions", 0) or 0),
        -_parse_decimal(item.get("realized_pnl_usd", "0"), "realized_pnl_usd"),
        str(item.get(key_name, "") or ""),
    )


def get_paper_strategy_history_summary(
    *,
    symbol: str = "",
    strategy_id: str = "",
    regime: str = "",
    outcome: str = "",
    start_date: str = "",
    end_date: str = "",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    history = get_paper_strategy_history(
        symbol=symbol,
        strategy_id=strategy_id,
        regime=regime,
        outcome=outcome,
        start_date=start_date,
        end_date=end_date,
        home=home,
    )

    total_bucket = {
        "closed_positions": 0,
        "wins": 0,
        "losses": 0,
        "flat": 0,
        "realized_pnl_usd": Decimal("0"),
        "outcomes": {},
    }
    strategy_buckets: dict[str, dict[str, Any]] = {}
    regime_buckets: dict[str, dict[str, Any]] = {}

    for record in history["records"]:
        _update_history_bucket(total_bucket, record)

        normalized_strategy = str(record.get("strategy_id", "") or "").strip().lower() or "unknown"
        strategy_bucket = strategy_buckets.setdefault(
            normalized_strategy,
            {
                "closed_positions": 0,
                "wins": 0,
                "losses": 0,
                "flat": 0,
                "realized_pnl_usd": Decimal("0"),
                "outcomes": {},
                "regimes": {},
            },
        )
        _update_history_bucket(strategy_bucket, record)

        primary_regime = str(record.get("primary_regime", "") or "").strip().lower() or "unknown"
        overall_regime_bucket = regime_buckets.setdefault(
            primary_regime,
            {
                "closed_positions": 0,
                "wins": 0,
                "losses": 0,
                "flat": 0,
                "realized_pnl_usd": Decimal("0"),
                "outcomes": {},
            },
        )
        _update_history_bucket(overall_regime_bucket, record)

        per_strategy_regime_bucket = strategy_bucket["regimes"].setdefault(
            primary_regime,
            {
                "closed_positions": 0,
                "wins": 0,
                "losses": 0,
                "flat": 0,
                "realized_pnl_usd": Decimal("0"),
                "outcomes": {},
            },
        )
        _update_history_bucket(per_strategy_regime_bucket, record)

    finalized_strategies: list[dict[str, Any]] = []
    for normalized_strategy, bucket in strategy_buckets.items():
        finalized_regimes = [
            _finalize_history_bucket("regime_label", regime_label, regime_bucket)
            for regime_label, regime_bucket in bucket["regimes"].items()
        ]
        finalized_regimes.sort(key=lambda item: _history_bucket_sort_key(item, "regime_label"))
        finalized_strategies.append(
            _finalize_history_bucket(
                "strategy_id",
                normalized_strategy,
                bucket,
                regimes=finalized_regimes,
            )
        )
    finalized_strategies.sort(key=lambda item: _history_bucket_sort_key(item, "strategy_id"))

    finalized_regimes = [
        _finalize_history_bucket("regime_label", regime_label, bucket)
        for regime_label, bucket in regime_buckets.items()
    ]
    finalized_regimes.sort(key=lambda item: _history_bucket_sort_key(item, "regime_label"))

    summary = _finalize_history_bucket("scope", "overall", total_bucket)
    summary.update(
        {
            "success": True,
            "filters": dict(history["filters"]),
            "total_matches": history["total_matches"],
            "strategies": finalized_strategies,
            "regimes": finalized_regimes,
        }
    )
    summary.pop("scope", None)
    return summary


def get_paper_daily_summary(summary_date: str = "", *, home: Optional[Path] = None) -> dict[str, Any]:
    wanted_date = str(summary_date or "").strip() or _now_utc().date().isoformat()
    state = _load_state(home=home)
    events = _iter_jsonl(get_paper_journal_path(home=home))
    entries: list[dict[str, Any]] = []
    exits: list[dict[str, Any]] = []
    approvals_requested = 0
    approvals_approved = 0
    approvals_denied = 0
    realized_pnl_usd = Decimal("0")

    for event in events:
        if event.get("event_type") == "paper_position_opened" and str(event.get("opened_at", "")).startswith(wanted_date):
            entries.append(
                {
                    "position_id": str(event.get("position_id", "") or "").strip(),
                    "symbol": str(event.get("symbol", "") or "").strip().upper(),
                    "side": str(event.get("side", "") or "").strip().upper(),
                    "opened_at": str(event.get("opened_at", "") or "").strip(),
                    "notional_usd": str(event.get("notional_usd", "0") or "0").strip(),
                }
            )
            continue
        if event.get("event_type") == "paper_position_closed" and str(event.get("closed_at", "")).startswith(wanted_date):
            pnl = _parse_decimal(event.get("realized_pnl_usd", "0"), "realized_pnl_usd")
            realized_pnl_usd += pnl
            exits.append(
                {
                    "position_id": str(event.get("position_id", "") or "").strip(),
                    "symbol": str(event.get("symbol", "") or "").strip().upper(),
                    "side": str(event.get("side", "") or "").strip().upper(),
                    "closed_at": str(event.get("closed_at", "") or "").strip(),
                    "realized_pnl_usd": _decimal_to_str(pnl),
                    "trigger": str(event.get("trigger", "") or "").strip(),
                    "trigger_category": str(event.get("trigger_category", "") or "").strip(),
                    "reason": str(event.get("reason", "") or "").strip(),
                    "thesis_outcome": str(event.get("thesis_outcome", "") or "").strip(),
                    "failure_mode": str(event.get("failure_mode", "") or "").strip(),
                }
            )
            continue
        if event.get("event_type") == "trade_approval_requested" and str(event.get("recorded_at", "")).startswith(wanted_date):
            approvals_requested += 1
            continue
        if event.get("event_type") == "trade_approval_recorded" and str(event.get("recorded_at", "")).startswith(wanted_date):
            status = str(event.get("status", "") or "").strip().lower()
            if status == "approved":
                approvals_approved += 1
            elif status == "denied":
                approvals_denied += 1

    open_positions = [PaperPosition.from_payload(payload) for payload in state.get("open_positions", [])]
    return {
        "success": True,
        "date": wanted_date,
        "entries_count": len(entries),
        "exits_count": len(exits),
        "approvals_requested": approvals_requested,
        "approvals_approved": approvals_approved,
        "approvals_denied": approvals_denied,
        "realized_pnl_usd": _decimal_to_str(realized_pnl_usd),
        "open_positions_count": len(open_positions),
        "open_positions": [position.to_dict() for position in open_positions],
        "entries": entries,
        "exits": exits,
        "strategy_scorecard": get_paper_strategy_history_summary(
            start_date=wanted_date,
            end_date=wanted_date,
            home=home,
        ),
    }


def seed_paper_account(
    starting_balance_usd: Optional[Decimal] = None,
    *,
    reset: bool = False,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    state_path = get_paper_state_path(home=home)
    amount = starting_balance_usd if starting_balance_usd is not None else _starting_balance_from_env()
    if reset or not state_path.exists():
        state = _default_state(amount)
        _save_state(state, home=home)
        _append_jsonl(
            get_paper_journal_path(home=home),
            {
                "event_type": "paper_account_seeded",
                "recorded_at": _now_iso(),
                "starting_balance_usd": _decimal_to_str(amount),
                "reset": bool(reset),
            },
        )
    return get_paper_account_overview(home=home)


def get_paper_account_overview(symbol: str = "", *, home: Optional[Path] = None) -> dict[str, Any]:
    state = _load_state(home=home)
    cash_balance_usd = _parse_decimal(state.get("cash_balance_usd", "0"), "cash_balance_usd")
    open_positions = state.get("open_positions", [])
    reserved_margin_usd = _reserved_margin_usd(open_positions)
    free_balance_usd = cash_balance_usd - reserved_margin_usd
    normalized_symbol = str(symbol or "").strip().upper()
    positions_in_symbol = sum(
        1 for payload in open_positions if str(payload.get("symbol", "")).strip().upper() == normalized_symbol
    ) if normalized_symbol else 0
    snapshot = BinanceAccountSnapshot.from_payload(
        {
            "free_balance_usd": _decimal_to_str(free_balance_usd),
            "open_positions": len(open_positions),
            "positions_in_symbol": positions_in_symbol,
            "daily_realized_pnl_usd": _decimal_to_str(_daily_realized_pnl_usd(home=home)),
            "kill_switch_active": False,
        }
    )
    return {
        "success": True,
        "execution_mode": "paper",
        "cash_balance_usd": _decimal_to_str(cash_balance_usd),
        "reserved_margin_usd": _decimal_to_str(reserved_margin_usd),
        "account_snapshot": snapshot.to_dict(),
        "open_positions": [payload for payload in open_positions if not normalized_symbol or payload.get("symbol") == normalized_symbol],
        "state_path": str(get_paper_state_path(home=home)),
        "journal_path": str(get_paper_journal_path(home=home)),
        "approvals_path": str(get_trade_approvals_path(home=home)),
        "evidence_path": str(get_market_evidence_path(home=home)),
    }


def record_market_evidence(
    *,
    symbol: str,
    timeframe: str,
    binance_reference_price: Decimal,
    external_reference_price: Decimal,
    market_summary: str,
    source_urls: Sequence[str] | str,
    external_source_name: str = "",
    momentum_summary: str = "",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        raise ValueError("symbol is required")
    urls = _split_source_urls(source_urls)
    invalid_urls = [value for value in urls if not _is_http_url(value)]
    if invalid_urls:
        raise ValueError(
            "source_urls must contain absolute http(s) URLs; invalid entries: "
            + ", ".join(invalid_urls[:3])
        )
    if normalized_symbol in _MULTI_SOURCE_SYMBOLS and len(urls) < 2:
        raise ValueError(f"{normalized_symbol} evidence requires at least two source URLs")
    if normalized_symbol in _MULTI_SOURCE_SYMBOLS and not _has_external_confirmation_url(urls):
        raise ValueError(f"{normalized_symbol} evidence requires at least one non-Binance confirmation URL")
    if binance_reference_price <= 0:
        raise ValueError("binance_reference_price must be greater than zero")
    if external_reference_price <= 0:
        raise ValueError("external_reference_price must be greater than zero")
    evidence = {
        "evidence_id": f"EVID-{uuid.uuid4().hex[:10].upper()}",
        "recorded_at": _now_iso(),
        "symbol": normalized_symbol,
        "timeframe": str(timeframe or "spot").strip() or "spot",
        "binance_reference_price": _decimal_to_str(binance_reference_price),
        "external_reference_price": _decimal_to_str(external_reference_price),
        "external_source_name": str(external_source_name or "").strip() or None,
        "market_summary": str(market_summary or "").strip(),
        "momentum_summary": str(momentum_summary or "").strip() or None,
        "source_urls": list(urls),
    }
    _append_jsonl(get_market_evidence_path(home=home), evidence)
    _append_jsonl(
        get_paper_journal_path(home=home),
        {
            "event_type": "market_evidence_recorded",
            "recorded_at": evidence["recorded_at"],
            "symbol": normalized_symbol,
            "evidence_id": evidence["evidence_id"],
            "source_count": len(urls),
        },
    )
    return evidence


def get_market_evidence(evidence_id: str, *, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    wanted = str(evidence_id or "").strip().upper()
    if not wanted:
        return None
    records = _iter_jsonl(get_market_evidence_path(home=home))
    for record in reversed(records):
        if str(record.get("evidence_id", "")).strip().upper() == wanted:
            return record
    return None


def _proposal_fingerprint(proposal: BinanceTradeProposal) -> str:
    payload = {
        "symbol": proposal.symbol,
        "side": proposal.side,
        "mode": proposal.mode,
        "order_type": proposal.order_type,
        "notional_usd": proposal.to_dict()["notional_usd"],
        "stop_loss_pct": proposal.to_dict().get("stop_loss_pct"),
        "take_profit_pct": proposal.to_dict().get("take_profit_pct"),
        "leverage": proposal.to_dict()["leverage"],
        "dry_run": proposal.dry_run,
    }
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16].upper()


def _approval_expired(record: dict[str, Any]) -> bool:
    expires_at = str(record.get("expires_at", "")).strip()
    if not expires_at:
        return False
    try:
        expiry = datetime.fromisoformat(expires_at)
    except ValueError:
        return False
    return _now_utc() >= expiry


def _approval_should_expire(record: dict[str, Any]) -> bool:
    status = str(record.get("status", "")).strip().lower()
    if status == "pending":
        return _approval_expired(record)
    if status == "approved" and not record.get("consumed_at"):
        return _approval_expired(record)
    return False


def _premium_request_expired(record: dict[str, Any]) -> bool:
    return _approval_expired(record)


def _material_fingerprint(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:16].upper()


def request_doge_premium_analysis(
    *,
    symbol: str,
    request_kind: str,
    model: str,
    material_payload: dict[str, Any],
    material_summary: str = "",
    requested_via: str = "cron_15m_doge",
    expires_minutes: int = 0,
    high_risk: bool = False,
    fallback_allowed: bool = True,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_kind = str(request_kind or "").strip().lower()
    normalized_model = str(model or "").strip()
    if normalized_symbol != "DOGEUSDT":
        raise ValueError("premium DOGE analysis only supports DOGEUSDT")
    if normalized_kind not in {"entry", "adjustment"}:
        raise ValueError("request_kind must be entry or adjustment")
    if not normalized_model:
        raise ValueError("model is required for premium DOGE analysis")
    if not isinstance(material_payload, dict) or not material_payload:
        raise ValueError("material_payload is required for premium DOGE analysis")

    request_store = _load_doge_premium_requests(home=home)
    ttl = expires_minutes if expires_minutes > 0 else _doge_premium_analysis_ttl_minutes()
    request_id = f"PREM-{uuid.uuid4().hex[:8].upper()}"
    event_fingerprint = _material_fingerprint(material_payload)
    record = {
        "request_id": request_id,
        "created_at": _now_iso(),
        "expires_at": (_now_utc() + timedelta(minutes=ttl)).isoformat(),
        "status": "pending",
        "analysis_outcome": None,
        "requested_via": str(requested_via or "cron_15m_doge").strip() or "cron_15m_doge",
        "symbol": normalized_symbol,
        "request_kind": normalized_kind,
        "model": normalized_model,
        "event_fingerprint": event_fingerprint,
        "material_summary": str(material_summary or "").strip() or None,
        "material_payload": material_payload,
        "high_risk": bool(high_risk),
        "fallback_allowed": bool(fallback_allowed),
        "response_text": None,
        "decision_by": None,
        "decided_at": None,
        "completed_at": None,
        "analysis": None,
    }
    request_store.setdefault("requests", []).append(record)
    _save_doge_premium_requests(request_store, home=home)
    _append_jsonl(
        get_paper_journal_path(home=home),
        {
            "event_type": "doge_premium_analysis_requested",
            "recorded_at": record["created_at"],
            "request_id": request_id,
            "symbol": normalized_symbol,
            "request_kind": normalized_kind,
            "event_fingerprint": event_fingerprint,
            "model": normalized_model,
            "high_risk": bool(high_risk),
        },
    )
    return record


def get_doge_premium_analysis_request(
    request_id: str,
    *,
    home: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    normalized_id = str(request_id or "").strip().upper()
    if not normalized_id:
        return None
    payload = _load_doge_premium_requests(home=home)
    dirty = False
    for record in payload.get("requests", []):
        if str(record.get("request_id", "")).strip().upper() != normalized_id:
            continue
        if record.get("status") == "pending" and _premium_request_expired(record):
            record["status"] = "expired"
            dirty = True
        if dirty:
            _save_doge_premium_requests(payload, home=home)
        return record
    return None


def get_latest_doge_premium_analysis_request(
    *,
    symbol: str = "",
    request_kind: str = "",
    status: str = "",
    event_fingerprint: str = "",
    home: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_kind = str(request_kind or "").strip().lower()
    normalized_status = str(status or "").strip().lower()
    normalized_fingerprint = str(event_fingerprint or "").strip().upper()
    payload = _load_doge_premium_requests(home=home)
    requests = payload.get("requests", [])
    dirty = False

    for record in requests:
        if record.get("status") == "pending" and _premium_request_expired(record):
            record["status"] = "expired"
            dirty = True

    if dirty:
        _save_doge_premium_requests(payload, home=home)

    for record in reversed(requests):
        record_symbol = str(record.get("symbol", "") or "").strip().upper()
        record_kind = str(record.get("request_kind", "") or "").strip().lower()
        record_status = str(record.get("status", "") or "").strip().lower()
        record_fingerprint = str(record.get("event_fingerprint", "") or "").strip().upper()
        if normalized_symbol and record_symbol != normalized_symbol:
            continue
        if normalized_kind and record_kind != normalized_kind:
            continue
        if normalized_status and record_status != normalized_status:
            continue
        if normalized_fingerprint and record_fingerprint != normalized_fingerprint:
            continue
        return record
    return None


def record_doge_premium_analysis_decision(
    request_id: str,
    *,
    decision: str,
    response_text: str = "",
    responder: str = "operator",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_id = str(request_id or "").strip().upper()
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision in {"approve", "approved", "yes", "y"}:
        next_status = "approved"
    elif normalized_decision in {"deny", "denied", "reject", "rejected", "cancel", "canceled", "no", "n"}:
        next_status = "denied"
    else:
        raise ValueError("decision must be approve or deny")

    payload = _load_doge_premium_requests(home=home)
    for record in payload.get("requests", []):
        if str(record.get("request_id", "")).strip().upper() != normalized_id:
            continue
        if _premium_request_expired(record):
            record["status"] = "expired"
            _save_doge_premium_requests(payload, home=home)
            raise ValueError(f"request_id '{normalized_id}' has expired")
        if record.get("status") != "pending":
            raise ValueError(f"request_id '{normalized_id}' is already {record.get('status')}")
        record["status"] = next_status
        record["response_text"] = str(response_text or "").strip() or None
        record["decision_by"] = str(responder or "operator").strip() or "operator"
        record["decided_at"] = _now_iso()
        _save_doge_premium_requests(payload, home=home)
        _append_jsonl(
            get_paper_journal_path(home=home),
            {
                "event_type": "doge_premium_analysis_recorded",
                "recorded_at": record["decided_at"],
                "request_id": normalized_id,
                "symbol": record.get("symbol"),
                "request_kind": record.get("request_kind"),
                "status": next_status,
            },
        )
        return record
    raise ValueError(f"request_id '{normalized_id}' was not found")


def complete_doge_premium_analysis_request(
    request_id: str,
    *,
    analysis_outcome: str,
    analysis: Optional[dict[str, Any]] = None,
    response_text: str = "",
    responder: str = "system",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_id = str(request_id or "").strip().upper()
    normalized_outcome = str(analysis_outcome or "").strip().lower()
    if normalized_outcome not in {"passed", "rejected", "error", "skipped"}:
        raise ValueError("analysis_outcome must be passed, rejected, error, or skipped")

    payload = _load_doge_premium_requests(home=home)
    for record in payload.get("requests", []):
        if str(record.get("request_id", "")).strip().upper() != normalized_id:
            continue
        if record.get("status") not in {"approved", "completed", "expired", "denied"}:
            raise ValueError(f"request_id '{normalized_id}' cannot be completed from status {record.get('status')}")
        record["status"] = "completed"
        record["analysis_outcome"] = normalized_outcome
        record["analysis"] = analysis or None
        record["response_text"] = str(response_text or record.get("response_text") or "").strip() or None
        record["decision_by"] = str(responder or record.get("decision_by") or "system").strip() or "system"
        record["completed_at"] = _now_iso()
        _save_doge_premium_requests(payload, home=home)
        _append_jsonl(
            get_paper_journal_path(home=home),
            {
                "event_type": "doge_premium_analysis_completed",
                "recorded_at": record["completed_at"],
                "request_id": normalized_id,
                "symbol": record.get("symbol"),
                "request_kind": record.get("request_kind"),
                "analysis_outcome": normalized_outcome,
            },
        )
        return record
    raise ValueError(f"request_id '{normalized_id}' was not found")


def request_trade_approval(
    proposal: BinanceTradeProposal,
    *,
    evidence_id: str = "",
    market_summary: str = "",
    requested_via: str = "whatsapp",
    expires_minutes: int = 0,
    decision_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    evidence = None
    normalized_evidence_id = str(evidence_id or "").strip().upper()
    if normalized_evidence_id:
        evidence = get_market_evidence(normalized_evidence_id, home=home)
        if evidence is None:
            raise ValueError(f"evidence_id '{normalized_evidence_id}' was not found")
    elif proposal.symbol in _MULTI_SOURCE_SYMBOLS:
        raise ValueError(f"evidence_id is required for {proposal.symbol} semi-autonomous entries")

    approval_store = _load_approvals(home=home)
    ttl = expires_minutes if expires_minutes > 0 else _trade_approval_ttl_minutes()
    approval_id = f"TRADE-{uuid.uuid4().hex[:8].upper()}"
    normalized_decision_context = _normalize_decision_context(decision_context)
    record = {
        "approval_id": approval_id,
        "created_at": _now_iso(),
        "expires_at": (_now_utc() + timedelta(minutes=ttl)).isoformat(),
        "status": "pending",
        "requested_via": str(requested_via or "whatsapp").strip() or "whatsapp",
        "proposal": proposal.to_dict(),
        "proposal_fingerprint": _proposal_fingerprint(proposal),
        "symbol": proposal.symbol,
        "evidence_id": evidence["evidence_id"] if evidence else None,
        "market_summary": str(market_summary or "").strip() or (evidence.get("market_summary") if evidence else ""),
        "source_urls": list(evidence.get("source_urls", [])) if evidence else [],
        "response_text": None,
        "decision_by": None,
        "decided_at": None,
        "consumed_at": None,
        "decision_context": normalized_decision_context,
    }
    approval_store.setdefault("approvals", []).append(record)
    _save_approvals(approval_store, home=home)
    _append_jsonl(
        get_paper_journal_path(home=home),
        {
            "event_type": "trade_approval_requested",
            "recorded_at": record["created_at"],
            "approval_id": approval_id,
            "symbol": proposal.symbol,
            "proposal_fingerprint": record["proposal_fingerprint"],
            "evidence_id": record["evidence_id"],
            "decision_context": normalized_decision_context,
        },
    )
    return record


def record_live_trade_execution_failure(
    *,
    proposal: BinanceTradeProposal,
    error: str,
    approval_id: str = "",
    stage: str = "submit_trade",
    rollback_sent: bool = False,
    details: Optional[dict[str, Any]] = None,
    decision_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    record = {
        "event_type": "live_trade_execution_failed",
        "recorded_at": _now_iso(),
        "approval_id": str(approval_id or "").strip().upper() or None,
        "symbol": proposal.symbol,
        "proposal_fingerprint": _proposal_fingerprint(proposal),
        "stage": str(stage or "submit_trade").strip() or "submit_trade",
        "rollback_sent": bool(rollback_sent),
        "error": str(error or "").strip() or "unknown live execution error",
        "details": _normalize_json_payload(details) if details else None,
        "decision_context": _normalize_decision_context(decision_context),
    }
    _append_jsonl(get_paper_journal_path(home=home), record)
    return record


def record_live_trade_execution_success(
    *,
    proposal: BinanceTradeProposal,
    execution: Mapping[str, Any],
    approval_id: str = "",
    evidence_id: str = "",
    details: Optional[dict[str, Any]] = None,
    decision_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    record = {
        "event_type": "live_trade_executed",
        "recorded_at": _now_iso(),
        "approval_id": str(approval_id or "").strip().upper() or None,
        "symbol": proposal.symbol,
        "proposal_fingerprint": _proposal_fingerprint(proposal),
        "evidence_id": str(evidence_id or "").strip().upper() or None,
        "proposal": proposal.to_dict(),
        "execution": _normalize_json_payload(execution),
        "details": _normalize_json_payload(details) if details else None,
        "decision_context": _normalize_decision_context(decision_context),
    }
    _append_jsonl(get_paper_journal_path(home=home), record)
    return record


def record_live_trade_protection_adjustment(
    *,
    symbol: str,
    approval_id: str = "",
    management: Mapping[str, Any],
    adjustment: Mapping[str, Any],
    premium_request: Optional[Mapping[str, Any]] = None,
    premium_assessment: Optional[Mapping[str, Any]] = None,
    decision_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    record = {
        "event_type": "live_trade_protection_adjusted",
        "recorded_at": _now_iso(),
        "symbol": str(symbol or "").strip().upper(),
        "approval_id": str(approval_id or "").strip().upper() or None,
        "management": _normalize_json_payload(management),
        "adjustment": _normalize_json_payload(adjustment),
        "premium_request_id": str((premium_request or {}).get("request_id") or "").strip() or None,
        "premium_assessment": _normalize_json_payload(premium_assessment) if premium_assessment else None,
        "decision_context": _normalize_decision_context(decision_context),
    }
    _append_jsonl(get_paper_journal_path(home=home), record)
    return record


def record_trade_approval(
    approval_id: str,
    *,
    decision: str,
    response_text: str = "",
    responder: str = "operator",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_id = str(approval_id or "").strip().upper()
    normalized_decision = str(decision or "").strip().lower()
    if normalized_decision in {"approve", "approved", "yes", "y"}:
        next_status = "approved"
    elif normalized_decision in {"deny", "denied", "reject", "rejected", "no", "n"}:
        next_status = "denied"
    else:
        raise ValueError("decision must be approve or deny")

    payload = _load_approvals(home=home)
    for record in payload.get("approvals", []):
        if str(record.get("approval_id", "")).strip().upper() != normalized_id:
            continue
        if _approval_expired(record):
            record["status"] = "expired"
            _save_approvals(payload, home=home)
            raise ValueError(f"approval_id '{normalized_id}' has expired")
        if record.get("status") != "pending":
            raise ValueError(f"approval_id '{normalized_id}' is already {record.get('status')}")
        record["status"] = next_status
        record["response_text"] = str(response_text or "").strip() or None
        record["decision_by"] = str(responder or "operator").strip() or "operator"
        record["decided_at"] = _now_iso()
        _save_approvals(payload, home=home)
        _append_jsonl(
            get_paper_journal_path(home=home),
            {
                "event_type": "trade_approval_recorded",
                "recorded_at": record["decided_at"],
                "approval_id": normalized_id,
                "symbol": record.get("symbol"),
                "status": next_status,
            },
        )
        return record
    raise ValueError(f"approval_id '{normalized_id}' was not found")


def get_trade_approval(approval_id: str, *, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    normalized_id = str(approval_id or "").strip().upper()
    if not normalized_id:
        return None
    payload = _load_approvals(home=home)
    dirty = False
    for record in payload.get("approvals", []):
        if str(record.get("approval_id", "")).strip().upper() != normalized_id:
            continue
        if _approval_should_expire(record):
            record["status"] = "expired"
            dirty = True
        if dirty:
            _save_approvals(payload, home=home)
        return record
    return None


def get_latest_trade_approval(
    *,
    symbol: str = "",
    status: str = "",
    home: Optional[Path] = None,
) -> Optional[dict[str, Any]]:
    normalized_symbol = str(symbol or "").strip().upper()
    normalized_status = str(status or "").strip().lower()
    payload = _load_approvals(home=home)
    approvals = payload.get("approvals", [])
    dirty = False

    for record in approvals:
        if _approval_should_expire(record):
            record["status"] = "expired"
            dirty = True

    if dirty:
        _save_approvals(payload, home=home)

    for record in reversed(approvals):
        record_symbol = str(record.get("symbol", "") or "").strip().upper()
        record_status = str(record.get("status", "") or "").strip().lower()
        if normalized_symbol and record_symbol != normalized_symbol:
            continue
        if normalized_status and record_status != normalized_status:
            continue
        return record
    return None


def validate_trade_approval(
    approval_id: str,
    proposal: BinanceTradeProposal,
    *,
    home: Optional[Path] = None,
) -> tuple[bool, str, Optional[dict[str, Any]]]:
    record = get_trade_approval(approval_id, home=home)
    normalized_id = str(approval_id or "").strip().upper()
    if record is None:
        return False, f"approval_id '{normalized_id}' was not found", None
    status = str(record.get("status", "")).strip().lower()
    if status != "approved":
        return False, f"approval_id '{normalized_id}' is not approved (status: {status or 'unknown'})", record
    if str(record.get("proposal_fingerprint", "")).strip().upper() != _proposal_fingerprint(proposal):
        return False, f"approval_id '{normalized_id}' does not match the proposed trade parameters", record
    return True, "", record


def consume_trade_approval(
    approval_id: str,
    proposal: BinanceTradeProposal,
    *,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    ok, error, record = validate_trade_approval(approval_id, proposal, home=home)
    if not ok or record is None:
        raise ValueError(error)
    payload = _load_approvals(home=home)
    normalized_id = str(approval_id or "").strip().upper()
    for existing in payload.get("approvals", []):
        if str(existing.get("approval_id", "")).strip().upper() != normalized_id:
            continue
        existing["status"] = "consumed"
        existing["consumed_at"] = _now_iso()
        _save_approvals(payload, home=home)
        _append_jsonl(
            get_paper_journal_path(home=home),
            {
                "event_type": "trade_approval_consumed",
                "recorded_at": existing["consumed_at"],
                "approval_id": normalized_id,
                "symbol": existing.get("symbol"),
            },
        )
        return existing
    raise ValueError(f"approval_id '{normalized_id}' was not found")


def _build_position(
    proposal: BinanceTradeProposal,
    *,
    reference_price: Decimal,
    approval_id: str = "",
    evidence_id: str = "",
    decision_context: Optional[Mapping[str, Any]] = None,
) -> PaperPosition:
    quantity = proposal.notional_usd / reference_price
    stop_loss_pct = proposal.stop_loss_pct or Decimal("0")
    take_profit_pct = proposal.take_profit_pct or Decimal("0")
    if proposal.side == "BUY":
        stop_loss_price = reference_price * (Decimal("1") - (stop_loss_pct / Decimal("100")))
        take_profit_price = reference_price * (Decimal("1") + (take_profit_pct / Decimal("100")))
    else:
        stop_loss_price = reference_price * (Decimal("1") + (stop_loss_pct / Decimal("100")))
        take_profit_price = reference_price * (Decimal("1") - (take_profit_pct / Decimal("100")))
    return PaperPosition(
        position_id=f"PPOS-{uuid.uuid4().hex[:10].upper()}",
        symbol=proposal.symbol,
        side=proposal.side,
        entry_price=reference_price,
        quantity=quantity,
        notional_usd=proposal.notional_usd,
        leverage=proposal.leverage,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        stop_loss_price=stop_loss_price,
        take_profit_price=take_profit_price,
        rationale=proposal.rationale,
        approval_id=str(approval_id or "").strip().upper() or None,
        evidence_id=str(evidence_id or "").strip().upper() or None,
        opened_at=_now_iso(),
        decision_context=_normalize_decision_context(decision_context),
    )


def open_paper_position(
    proposal: BinanceTradeProposal,
    *,
    reference_price: Decimal,
    approval_id: str = "",
    evidence_id: str = "",
    decision_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    state = _load_state(home=home)
    position = _build_position(
        proposal,
        reference_price=reference_price,
        approval_id=approval_id,
        evidence_id=evidence_id,
        decision_context=decision_context,
    )
    state.setdefault("open_positions", []).append(position.to_dict())
    _save_state(state, home=home)
    overview = get_paper_account_overview(symbol=proposal.symbol, home=home)
    _append_jsonl(
        get_paper_journal_path(home=home),
        {
            "event_type": "paper_position_opened",
            "opened_at": position.opened_at,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "entry_price": _decimal_to_str(position.entry_price),
            "quantity": _decimal_to_str(position.quantity),
            "notional_usd": _decimal_to_str(position.notional_usd),
            "leverage": _decimal_to_str(position.leverage),
            "stop_loss_pct": _decimal_to_str(position.stop_loss_pct),
            "take_profit_pct": _decimal_to_str(position.take_profit_pct),
            "stop_loss_price": _decimal_to_str(position.stop_loss_price),
            "take_profit_price": _decimal_to_str(position.take_profit_price),
            "approval_id": position.approval_id,
            "evidence_id": position.evidence_id,
            "decision_context": position.decision_context,
        },
    )
    return {
        "position": position.to_dict(),
        "filled_at": position.opened_at,
        "fill_reference_price": _decimal_to_str(position.entry_price),
        "risk": _position_risk_summary(position),
        "commands": _follow_up_commands(position),
        "account_snapshot": overview["account_snapshot"],
        "journal_path": overview["journal_path"],
    }


def get_open_paper_position(position_id: str, *, home: Optional[Path] = None) -> Optional[dict[str, Any]]:
    normalized_id = str(position_id or "").strip().upper()
    if not normalized_id:
        return None
    state = _load_state(home=home)
    for payload in state.get("open_positions", []):
        if str(payload.get("position_id", "")).strip().upper() == normalized_id:
            return payload
    return None


def get_paper_position_status(
    reference_id: str = "",
    *,
    position_id: str = "",
    approval_id: str = "",
    reference_price: Optional[Decimal] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_reference = str(reference_id or "").strip().upper()
    normalized_position_id = str(position_id or "").strip().upper()
    normalized_approval_id = str(approval_id or "").strip().upper()
    if normalized_reference and not normalized_position_id and not normalized_approval_id:
        if normalized_reference.startswith("PPOS-"):
            normalized_position_id = normalized_reference
        elif normalized_reference.startswith("TRADE-"):
            normalized_approval_id = normalized_reference
    if not normalized_position_id and not normalized_approval_id:
        raise ValueError("reference_id, position_id, or approval_id is required")

    state = _load_state(home=home)
    for payload in state.get("open_positions", []):
        candidate_position_id = str(payload.get("position_id", "")).strip().upper()
        candidate_approval_id = str(payload.get("approval_id", "") or "").strip().upper()
        if normalized_position_id and candidate_position_id != normalized_position_id:
            continue
        if normalized_approval_id and candidate_approval_id != normalized_approval_id:
            continue
        position = PaperPosition.from_payload(payload)
        return {
            "success": True,
            **_position_status_payload(
                position,
                status="open",
                market_price=reference_price,
            ),
        }

    records = _iter_jsonl(get_paper_journal_path(home=home))
    for record in reversed(records):
        if record.get("event_type") != "paper_position_closed":
            continue
        candidate_position_id = str(record.get("position_id", "")).strip().upper()
        candidate_approval_id = str(record.get("approval_id", "") or "").strip().upper()
        if normalized_position_id and candidate_position_id != normalized_position_id:
            continue
        if normalized_approval_id and candidate_approval_id != normalized_approval_id:
            continue
        position = PaperPosition.from_payload(record)
        exit_price = _parse_decimal(record.get("exit_price", "0"), "exit_price")
        realized_pnl_usd = _parse_decimal(record.get("realized_pnl_usd", "0"), "realized_pnl_usd")
        return {
            "success": True,
            **_position_status_payload(
                position,
                status="closed",
                exit_price=exit_price,
                realized_pnl_usd=realized_pnl_usd,
                closed_at=str(record.get("closed_at", "") or "").strip(),
                trigger=str(record.get("trigger", "") or "").strip(),
                reason=str(record.get("reason", "") or "").strip(),
                trigger_category=str(record.get("trigger_category", "") or "").strip(),
                thesis_outcome=str(record.get("thesis_outcome", "") or "").strip(),
                thesis_outcome_tags=tuple(record.get("thesis_outcome_tags") or ()),
                failure_mode=str(record.get("failure_mode", "") or "").strip(),
                attribution_bucket=str(record.get("attribution_bucket", "") or "").strip(),
                expected_vs_realized=record.get("expected_vs_realized") or None,
                management_context=record.get("management_context") or None,
                execution_context=record.get("execution_context") or None,
            ),
        }

    wanted = normalized_position_id or normalized_approval_id or normalized_reference
    return {
        "success": False,
        "error": f"no paper position matched '{wanted}'",
    }


def close_paper_position(
    position_id: str,
    *,
    exit_price: Decimal,
    reason: str,
    trigger: str = "manual",
    thesis_outcome: str = "",
    failure_mode: str = "",
    management_context: Optional[Mapping[str, Any]] = None,
    execution_context: Optional[Mapping[str, Any]] = None,
    home: Optional[Path] = None,
) -> dict[str, Any]:
    normalized_id = str(position_id or "").strip().upper()
    state = _load_state(home=home)
    open_positions = state.get("open_positions", [])
    for index, payload in enumerate(open_positions):
        if str(payload.get("position_id", "")).strip().upper() != normalized_id:
            continue
        position = PaperPosition.from_payload(payload)
        pnl_per_unit = exit_price - position.entry_price
        if position.side == "SELL":
            pnl_per_unit = position.entry_price - exit_price
        realized_pnl_usd = pnl_per_unit * position.quantity
        cash_balance_usd = _parse_decimal(state.get("cash_balance_usd", "0"), "cash_balance_usd")
        state["cash_balance_usd"] = _decimal_to_str(cash_balance_usd + realized_pnl_usd)
        open_positions.pop(index)
        _save_state(state, home=home)
        closed_at = _now_iso()
        overview = get_paper_account_overview(symbol=position.symbol, home=home)
        duration = _duration_snapshot(position.opened_at, closed_at)
        risk = _position_risk_summary(position)
        attribution = _derive_exit_attribution(
            position=position,
            realized_pnl_usd=realized_pnl_usd,
            trigger=trigger,
            reason=reason,
            risk=risk,
            duration=duration,
            thesis_outcome=thesis_outcome,
            failure_mode=failure_mode,
        )
        record = {
            "event_type": "paper_position_closed",
            "closed_at": closed_at,
            "opened_at": position.opened_at,
            "position_id": position.position_id,
            "symbol": position.symbol,
            "side": position.side,
            "entry_price": _decimal_to_str(position.entry_price),
            "exit_price": _decimal_to_str(exit_price),
            "quantity": _decimal_to_str(position.quantity),
            "notional_usd": _decimal_to_str(position.notional_usd),
            "leverage": _decimal_to_str(position.leverage),
            "stop_loss_pct": _decimal_to_str(position.stop_loss_pct),
            "take_profit_pct": _decimal_to_str(position.take_profit_pct),
            "stop_loss_price": _decimal_to_str(position.stop_loss_price),
            "take_profit_price": _decimal_to_str(position.take_profit_price),
            "realized_pnl_usd": _decimal_to_str(realized_pnl_usd),
            "realized_pnl_pct": _pnl_pct_from_notional(
                pnl_usd=realized_pnl_usd,
                notional_usd=position.notional_usd,
            ),
            "trigger": str(trigger or "manual").strip() or "manual",
            "reason": str(reason or "").strip() or "manual close",
            "approval_id": position.approval_id,
            "evidence_id": position.evidence_id,
            "decision_context": position.decision_context,
            "duration_seconds": duration["duration_seconds"],
            "duration_human": duration["duration_human"],
            "estimated_max_loss_usd": risk["estimated_max_loss_usd"],
            "estimated_max_profit_usd": risk["estimated_max_profit_usd"],
            "risk_reward_ratio": risk["risk_reward_ratio"],
            "trigger_category": attribution["trigger_category"],
            "thesis_outcome": attribution["thesis_outcome"],
            "thesis_outcome_tags": attribution["thesis_outcome_tags"],
            "failure_mode": attribution["failure_mode"],
            "attribution_bucket": attribution["attribution_bucket"],
            "expected_vs_realized": attribution["expected_vs_realized"],
            "management_context": _normalize_json_payload(management_context) if management_context else None,
            "execution_context": _normalize_json_payload(execution_context) if execution_context else None,
        }
        _append_jsonl(get_paper_journal_path(home=home), record)
        return {
            "position": position.to_dict(),
            "exit_price": _decimal_to_str(exit_price),
            "realized_pnl_usd": _decimal_to_str(realized_pnl_usd),
            "realized_pnl_pct": record["realized_pnl_pct"],
            "trigger": record["trigger"],
            "reason": record["reason"],
            "closed_at": closed_at,
            "risk": risk,
            "duration_seconds": duration["duration_seconds"],
            "duration_human": duration["duration_human"],
            "commands": _follow_up_commands(position),
            "account_snapshot": overview["account_snapshot"],
            "journal_path": overview["journal_path"],
            "trigger_category": record["trigger_category"],
            "thesis_outcome": record["thesis_outcome"],
            "thesis_outcome_tags": list(record["thesis_outcome_tags"]),
            "failure_mode": record["failure_mode"],
            "attribution_bucket": record["attribution_bucket"],
            "expected_vs_realized": dict(record["expected_vs_realized"]),
            "management_context": record["management_context"],
            "execution_context": record["execution_context"],
        }
    raise ValueError(f"position_id '{normalized_id}' was not found")


def reconcile_protective_exits(
    price_lookup: Callable[[str], Decimal],
    *,
    symbol: str = "",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    state = _load_state(home=home)
    normalized_symbol = str(symbol or "").strip().upper()
    candidates = [PaperPosition.from_payload(payload) for payload in state.get("open_positions", [])]
    if normalized_symbol:
        candidates = [position for position in candidates if position.symbol == normalized_symbol]

    if not candidates:
        overview = get_paper_account_overview(symbol=normalized_symbol, home=home)
        return {
            "success": True,
            "closed_positions": [],
            "account_snapshot": overview["account_snapshot"],
        }

    cached_prices: dict[str, Decimal] = {}
    closed_positions: list[dict[str, Any]] = []
    for position in candidates:
        if position.symbol not in cached_prices:
            cached_prices[position.symbol] = price_lookup(position.symbol)
        current_price = cached_prices[position.symbol]
        trigger: Optional[str] = None
        if position.side == "BUY":
            if current_price <= position.stop_loss_price:
                trigger = "stop_loss"
            elif current_price >= position.take_profit_price:
                trigger = "take_profit"
        else:
            if current_price >= position.stop_loss_price:
                trigger = "stop_loss"
            elif current_price <= position.take_profit_price:
                trigger = "take_profit"
        if trigger is None:
            continue
        closed_positions.append(
            close_paper_position(
                position.position_id,
                exit_price=current_price,
                reason=f"protective exit triggered via {trigger}",
                trigger=trigger,
                management_context={
                    "reconciler": "protective_exit",
                    "market_price": _decimal_to_str(current_price),
                },
                home=home,
            )
        )

    overview = get_paper_account_overview(symbol=normalized_symbol, home=home)
    return {
        "success": True,
        "closed_positions": closed_positions,
        "account_snapshot": overview["account_snapshot"],
    }