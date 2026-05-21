from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Optional, Sequence
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
                    "reason": str(event.get("reason", "") or "").strip(),
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
        },
    )
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
    )


def open_paper_position(
    proposal: BinanceTradeProposal,
    *,
    reference_price: Decimal,
    approval_id: str = "",
    evidence_id: str = "",
    home: Optional[Path] = None,
) -> dict[str, Any]:
    state = _load_state(home=home)
    position = _build_position(
        proposal,
        reference_price=reference_price,
        approval_id=approval_id,
        evidence_id=evidence_id,
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
            "duration_seconds": duration["duration_seconds"],
            "duration_human": duration["duration_human"],
            "estimated_max_loss_usd": risk["estimated_max_loss_usd"],
            "estimated_max_profit_usd": risk["estimated_max_profit_usd"],
            "risk_reward_ratio": risk["risk_reward_ratio"],
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
                home=home,
            )
        )

    overview = get_paper_account_overview(symbol=normalized_symbol, home=home)
    return {
        "success": True,
        "closed_positions": closed_positions,
        "account_snapshot": overview["account_snapshot"],
    }