#!/usr/bin/env python3
"""Mandatory guardrails for a paper-first Binance control surface.

The goal of this module is to keep the exchange-facing surface narrow and
testable before any live execution path is wired in. A proposal must pass
these checks before it can graduate from analysis to execution.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping, Optional

import os

from hermes_constants import get_hermes_home


_VALID_SIDES = {"BUY", "SELL"}
_VALID_ORDER_TYPES = {"MARKET", "LIMIT"}
_VALID_MODES = {"paper", "live"}
_DEFAULT_ALLOWED_SYMBOLS = ("BTCUSDT", "ETHUSDT", "SOLUSDT")
_DEFAULT_VERIFIER_ALLOWLIST = (
    "gemini-3.1-flash-lite",
    "gemini-3.1-flash",
)


def _parse_decimal(value: Any, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal value") from exc


def _parse_optional_decimal(value: Any, field_name: str) -> Optional[Decimal]:
    if value in (None, ""):
        return None
    return _parse_decimal(value, field_name)


def _parse_bool(value: Any, *, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_int(value: Any, field_name: str) -> int:
    try:
        return int(str(value).strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def _parse_csv(value: Any, *, uppercase: bool = False) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, (list, tuple, set)):
        raw_items = [str(item).strip() for item in value]
    else:
        raw_items = [part.strip() for part in str(value).split(",")]
    items = []
    for item in raw_items:
        if not item:
            continue
        normalized = item.upper() if uppercase else item
        if normalized not in items:
            items.append(normalized)
    return tuple(items)


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    return format(value.normalize(), "f") if value != value.to_integral() else str(value.quantize(Decimal("1")))


def get_kill_switch_path(home: Optional[Path] = None) -> Path:
    hermes_home = Path(home) if home is not None else get_hermes_home()
    hermes_home.mkdir(parents=True, exist_ok=True)
    return hermes_home / "binance-kill-switch"


def is_kill_switch_active(home: Optional[Path] = None) -> bool:
    if _parse_bool(os.getenv("BINANCE_KILL_SWITCH"), default=False):
        return True
    return get_kill_switch_path(home=home).exists()


def set_kill_switch(enabled: bool, reason: str = "", home: Optional[Path] = None) -> dict[str, Any]:
    kill_switch_path = get_kill_switch_path(home=home)
    if enabled:
        kill_switch_path.write_text(
            "enabled_at="
            f"{datetime.now(timezone.utc).isoformat()}\n"
            f"reason={reason.strip() or 'manual'}\n",
            encoding="utf-8",
        )
    elif kill_switch_path.exists():
        kill_switch_path.unlink()
    return {
        "enabled": enabled,
        "path": str(kill_switch_path),
        "reason": reason.strip() or None,
    }


@dataclass(frozen=True)
class BinanceRiskLimits:
    mode: str = "paper"
    live_trading_enabled: bool = False
    allowed_symbols: tuple[str, ...] = _DEFAULT_ALLOWED_SYMBOLS
    allowed_order_types: tuple[str, ...] = tuple(sorted(_VALID_ORDER_TYPES))
    max_notional_usd: Decimal = Decimal("250")
    min_free_balance_usd: Decimal = Decimal("50")
    max_daily_loss_usd: Decimal = Decimal("75")
    max_open_positions: int = 2
    max_positions_per_symbol: int = 1
    max_leverage: Decimal = Decimal("1")
    require_stop_loss: bool = True
    require_take_profit: bool = True
    require_verifier: bool = True
    min_verifier_confidence: Decimal = Decimal("0.60")
    verifier_model_allowlist: tuple[str, ...] = _DEFAULT_VERIFIER_ALLOWLIST

    @classmethod
    def from_env(cls, env: Optional[Mapping[str, Any]] = None) -> "BinanceRiskLimits":
        env_map = env or os.environ
        mode = str(env_map.get("BINANCE_RISK_MODE", "paper")).strip().lower() or "paper"
        if mode not in _VALID_MODES:
            raise ValueError(f"Unsupported BINANCE_RISK_MODE: {mode}")

        allowed_symbols = _parse_csv(
            env_map.get("BINANCE_RISK_ALLOWED_SYMBOLS", ",".join(_DEFAULT_ALLOWED_SYMBOLS)),
            uppercase=True,
        ) or _DEFAULT_ALLOWED_SYMBOLS
        allowed_order_types = _parse_csv(
            env_map.get("BINANCE_RISK_ALLOWED_ORDER_TYPES", ",".join(sorted(_VALID_ORDER_TYPES))),
            uppercase=True,
        ) or tuple(sorted(_VALID_ORDER_TYPES))
        verifier_model_allowlist = _parse_csv(
            env_map.get("BINANCE_RISK_VERIFIER_MODELS", ",".join(_DEFAULT_VERIFIER_ALLOWLIST)),
            uppercase=False,
        ) or _DEFAULT_VERIFIER_ALLOWLIST

        return cls(
            mode=mode,
            live_trading_enabled=_parse_bool(env_map.get("BINANCE_LIVE_TRADING_ENABLED"), default=False),
            allowed_symbols=allowed_symbols,
            allowed_order_types=allowed_order_types,
            max_notional_usd=_parse_decimal(env_map.get("BINANCE_RISK_MAX_NOTIONAL_USD", "250"), "BINANCE_RISK_MAX_NOTIONAL_USD"),
            min_free_balance_usd=_parse_decimal(env_map.get("BINANCE_RISK_MIN_FREE_BALANCE_USD", "50"), "BINANCE_RISK_MIN_FREE_BALANCE_USD"),
            max_daily_loss_usd=_parse_decimal(env_map.get("BINANCE_RISK_MAX_DAILY_LOSS_USD", "75"), "BINANCE_RISK_MAX_DAILY_LOSS_USD"),
            max_open_positions=_parse_int(env_map.get("BINANCE_RISK_MAX_OPEN_POSITIONS", "2"), "BINANCE_RISK_MAX_OPEN_POSITIONS"),
            max_positions_per_symbol=_parse_int(env_map.get("BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL", "1"), "BINANCE_RISK_MAX_POSITIONS_PER_SYMBOL"),
            max_leverage=_parse_decimal(env_map.get("BINANCE_RISK_MAX_LEVERAGE", "1"), "BINANCE_RISK_MAX_LEVERAGE"),
            require_stop_loss=_parse_bool(env_map.get("BINANCE_RISK_REQUIRE_STOP_LOSS"), default=True),
            require_take_profit=_parse_bool(env_map.get("BINANCE_RISK_REQUIRE_TAKE_PROFIT"), default=True),
            require_verifier=_parse_bool(env_map.get("BINANCE_RISK_REQUIRE_VERIFIER"), default=True),
            min_verifier_confidence=_parse_decimal(env_map.get("BINANCE_RISK_MIN_VERIFIER_CONFIDENCE", "0.60"), "BINANCE_RISK_MIN_VERIFIER_CONFIDENCE"),
            verifier_model_allowlist=verifier_model_allowlist,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "live_trading_enabled": self.live_trading_enabled,
            "allowed_symbols": list(self.allowed_symbols),
            "allowed_order_types": list(self.allowed_order_types),
            "max_notional_usd": _decimal_to_str(self.max_notional_usd),
            "min_free_balance_usd": _decimal_to_str(self.min_free_balance_usd),
            "max_daily_loss_usd": _decimal_to_str(self.max_daily_loss_usd),
            "max_open_positions": self.max_open_positions,
            "max_positions_per_symbol": self.max_positions_per_symbol,
            "max_leverage": _decimal_to_str(self.max_leverage),
            "require_stop_loss": self.require_stop_loss,
            "require_take_profit": self.require_take_profit,
            "require_verifier": self.require_verifier,
            "min_verifier_confidence": _decimal_to_str(self.min_verifier_confidence),
            "verifier_model_allowlist": list(self.verifier_model_allowlist),
        }


@dataclass(frozen=True)
class BinanceTradeProposal:
    symbol: str
    side: str
    notional_usd: Decimal
    mode: str = "paper"
    order_type: str = "MARKET"
    stop_loss_pct: Optional[Decimal] = None
    take_profit_pct: Optional[Decimal] = None
    leverage: Decimal = Decimal("1")
    rationale: str = ""
    verifier_model: Optional[str] = None
    verifier_passed: bool = False
    verifier_confidence: Optional[Decimal] = None
    dry_run: bool = True
    macro_alignment: str = "aligned"

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BinanceTradeProposal":
        symbol = str(payload.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError("symbol is required")

        side = str(payload.get("side", "")).strip().upper()
        if side not in _VALID_SIDES:
            raise ValueError(f"side must be one of {sorted(_VALID_SIDES)}")

        mode = str(payload.get("mode", "paper")).strip().lower() or "paper"
        if mode not in _VALID_MODES:
            raise ValueError(f"mode must be one of {sorted(_VALID_MODES)}")

        order_type = str(payload.get("order_type", "MARKET")).strip().upper() or "MARKET"

        return cls(
            symbol=symbol,
            side=side,
            notional_usd=_parse_decimal(payload.get("notional_usd", "0"), "notional_usd"),
            mode=mode,
            order_type=order_type,
            stop_loss_pct=_parse_optional_decimal(payload.get("stop_loss_pct"), "stop_loss_pct"),
            take_profit_pct=_parse_optional_decimal(payload.get("take_profit_pct"), "take_profit_pct"),
            leverage=_parse_decimal(payload.get("leverage", "1"), "leverage"),
            rationale=str(payload.get("rationale", "") or "").strip(),
            verifier_model=str(payload.get("verifier_model", "") or "").strip() or None,
            verifier_passed=_parse_bool(payload.get("verifier_passed"), default=False),
            verifier_confidence=_parse_optional_decimal(payload.get("verifier_confidence"), "verifier_confidence"),
            dry_run=_parse_bool(payload.get("dry_run"), default=True),
            macro_alignment=str(payload.get("macro_alignment", "aligned")).strip().lower(),
)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "side": self.side,
            "notional_usd": _decimal_to_str(self.notional_usd),
            "mode": self.mode,
            "order_type": self.order_type,
            "stop_loss_pct": _decimal_to_str(self.stop_loss_pct),
            "take_profit_pct": _decimal_to_str(self.take_profit_pct),
            "leverage": _decimal_to_str(self.leverage),
            "rationale": self.rationale,
            "verifier_model": self.verifier_model,
            "verifier_passed": self.verifier_passed,
            "verifier_confidence": _decimal_to_str(self.verifier_confidence),
            "dry_run": self.dry_run,
            "macro_alignment": self.macro_alignment,
}


@dataclass(frozen=True)
class BinanceAccountSnapshot:
    free_balance_usd: Decimal = Decimal("0")
    open_positions: int = 0
    positions_in_symbol: int = 0
    daily_realized_pnl_usd: Decimal = Decimal("0")
    kill_switch_active: bool = False

    @classmethod
    def from_payload(cls, payload: Optional[Mapping[str, Any]] = None) -> "BinanceAccountSnapshot":
        data = payload or {}
        return cls(
            free_balance_usd=_parse_decimal(data.get("free_balance_usd", "0"), "free_balance_usd"),
            open_positions=_parse_int(data.get("open_positions", "0"), "open_positions"),
            positions_in_symbol=_parse_int(data.get("positions_in_symbol", "0"), "positions_in_symbol"),
            daily_realized_pnl_usd=_parse_decimal(data.get("daily_realized_pnl_usd", "0"), "daily_realized_pnl_usd"),
            kill_switch_active=_parse_bool(data.get("kill_switch_active"), default=False),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "free_balance_usd": _decimal_to_str(self.free_balance_usd),
            "open_positions": self.open_positions,
            "positions_in_symbol": self.positions_in_symbol,
            "daily_realized_pnl_usd": _decimal_to_str(self.daily_realized_pnl_usd),
            "kill_switch_active": self.kill_switch_active,
        }


@dataclass(frozen=True)
class BinanceRiskDecision:
    allowed: bool
    reasons: tuple[str, ...]
    proposal: BinanceTradeProposal
    account: BinanceAccountSnapshot
    limits: BinanceRiskLimits

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reasons": list(self.reasons),
            "proposal": self.proposal.to_dict(),
            "account": self.account.to_dict(),
            "limits": self.limits.to_dict(),
        }


def evaluate_trade_proposal(
    proposal: BinanceTradeProposal,
    account: BinanceAccountSnapshot,
    limits: BinanceRiskLimits,
    *,
    kill_switch_active: Optional[bool] = None,
) -> BinanceRiskDecision:
    reasons: list[str] = []

    if proposal.mode != limits.mode:
        reasons.append(
            f"proposal mode '{proposal.mode}' does not match active risk mode '{limits.mode}'"
        )
    if proposal.symbol not in limits.allowed_symbols:
        reasons.append(f"symbol '{proposal.symbol}' is outside the allowlist")
    if proposal.order_type not in limits.allowed_order_types:
        reasons.append(
            f"order_type '{proposal.order_type}' is not permitted by the risk profile"
        )
    if proposal.mode == "live" and not limits.live_trading_enabled:
        reasons.append("live trading is disabled in the active risk profile")
    if proposal.notional_usd <= 0:
        reasons.append("notional_usd must be greater than zero")
    # Dynamic macro sizing check
    effective_max_notional = limits.max_notional_usd
    if proposal.macro_alignment == "divergent":
        effective_max_notional = limits.max_notional_usd * Decimal("0.5")  # Risk Slash 50%
        
    if proposal.notional_usd > effective_max_notional:
        reasons.append(
            f"notional_usd {proposal.notional_usd} exceeds effective_max_notional {effective_max_notional} (Macro Alignment: {proposal.macro_alignment})"
        )
    if proposal.leverage <= 0:
        reasons.append("leverage must be greater than zero")
    if proposal.leverage > limits.max_leverage:
        reasons.append(
            f"leverage {proposal.leverage} exceeds max_leverage {limits.max_leverage}"
        )

    active_kill_switch = (
        is_kill_switch_active() if kill_switch_active is None else kill_switch_active
    ) or account.kill_switch_active
    if active_kill_switch:
        reasons.append("kill switch is active")

    if account.open_positions >= limits.max_open_positions:
        reasons.append(
            f"open_positions {account.open_positions} meets or exceeds max_open_positions {limits.max_open_positions}"
        )
    if account.positions_in_symbol >= limits.max_positions_per_symbol:
        reasons.append(
            "positions_in_symbol meets or exceeds max_positions_per_symbol"
        )
    if account.daily_realized_pnl_usd <= (limits.max_daily_loss_usd * Decimal("-1")):
        reasons.append(
            "daily realized PnL is below the allowed drawdown limit"
        )
    if (account.free_balance_usd - proposal.notional_usd) < limits.min_free_balance_usd:
        reasons.append(
            "proposal would breach the minimum free balance reserve"
        )

    if limits.require_stop_loss:
        if proposal.stop_loss_pct is None:
            reasons.append("stop_loss_pct is required")
        elif proposal.stop_loss_pct <= 0:
            reasons.append("stop_loss_pct must be greater than zero")
    elif proposal.stop_loss_pct is not None and proposal.stop_loss_pct <= 0:
        reasons.append("stop_loss_pct must be greater than zero when supplied")

    if limits.require_take_profit:
        if proposal.take_profit_pct is None:
            reasons.append("take_profit_pct is required")
        elif proposal.take_profit_pct <= 0:
            reasons.append("take_profit_pct must be greater than zero")
    elif proposal.take_profit_pct is not None and proposal.take_profit_pct <= 0:
        reasons.append("take_profit_pct must be greater than zero when supplied")

    if limits.require_verifier:
        if not proposal.verifier_passed:
            reasons.append("verifier_passed must be true before execution")
        if not proposal.verifier_model:
            reasons.append("verifier_model is required")
        elif limits.verifier_model_allowlist and proposal.verifier_model.lower() not in {
            value.lower() for value in limits.verifier_model_allowlist
        }:
            reasons.append(
                f"verifier_model '{proposal.verifier_model}' is outside the approved verifier allowlist"
            )
        if proposal.verifier_confidence is None:
            reasons.append("verifier_confidence is required")
        elif proposal.verifier_confidence < limits.min_verifier_confidence:
            reasons.append(
                "verifier_confidence is below the configured minimum"
            )

    return BinanceRiskDecision(
        allowed=not reasons,
        reasons=tuple(reasons),
        proposal=proposal,
        account=account,
        limits=limits,
    )