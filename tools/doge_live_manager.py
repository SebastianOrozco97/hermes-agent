from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping, Optional

from tools.binance_live_adapter import BinanceFuturesLiveExecutor
from tools.binance_paper_runtime import get_latest_trade_approval
from tools.doge_signal_engine import analyze_doge_15m_signal, parse_binance_klines
from tools.doge_trade_advisor import DogeTradeManagementPlan, plan_doge_management


def _decimal(value: Any, default: str = "0") -> Decimal:
    if value in (None, ""):
        value = default
    return Decimal(str(value).strip())


def _analyze_timeframe(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    interval: str,
    score_threshold: int,
) -> Any:
    raw_klines = executor.get_klines(symbol, interval=interval, limit=120)
    closed_klines = raw_klines[:-1] if len(raw_klines) > 1 else raw_klines
    return analyze_doge_15m_signal(
        parse_binance_klines(closed_klines),
        score_threshold=score_threshold,
        timeframe=interval,
    )


def _find_active_position(overview: Mapping[str, Any], symbol: str) -> Optional[dict[str, Any]]:
    wanted = str(symbol or "").strip().upper()
    for position in overview.get("active_positions") or []:
        if str(position.get("symbol", "") or "").strip().upper() != wanted:
            continue
        amount = _decimal(position.get("position_amt"), "0")
        if amount == 0:
            continue
        return dict(position)
    return None


def _entry_side_from_position(position: Mapping[str, Any]) -> str:
    return "BUY" if str(position.get("side") or "LONG").strip().upper() == "LONG" else "SELL"


def _supports_protective_adjustment(action: str) -> bool:
    normalized = str(action or "").strip().lower()
    return normalized in {
        "raise_stop_breakeven",
        "tighten_stop",
        "trail_profit",
        "trail_and_extend",
    }


@dataclass(frozen=True)
class DogeLiveManagementSnapshot:
    symbol: str
    timeframe: str
    signal: Any
    contextual_signals: Mapping[str, Any]
    active_position: Mapping[str, Any]
    approval: Mapping[str, Any]
    entry_side: str
    stop_loss_pct: Decimal
    take_profit_pct: Decimal
    price_tick: Decimal
    plan: DogeTradeManagementPlan
    protective_orders: Mapping[str, Any]
    protective_orders_missing: bool
    current_stop_price: Decimal
    current_take_profit_price: Decimal
    recommended_stop_price: Decimal
    recommended_take_profit_price: Decimal

    @property
    def approval_id(self) -> str:
        return str(self.approval.get("approval_id") or "").strip().upper()

    @property
    def actionable_adjustment(self) -> bool:
        if self.protective_orders_missing:
            return True
        if not _supports_protective_adjustment(self.plan.action):
            return False
        return (
            self.current_stop_price != self.recommended_stop_price
            or self.current_take_profit_price != self.recommended_take_profit_price
        )


def build_doge_live_management_snapshot(
    executor: BinanceFuturesLiveExecutor,
    *,
    symbol: str,
    timeframe: str = "15m",
    score_threshold: int = 5,
    context_timeframes: tuple[str, ...] = ("1h", "4h"),
    default_stop_loss_pct: Decimal = Decimal("0.5"),
    default_take_profit_pct: Decimal = Decimal("1.0"),
    overview: Optional[Mapping[str, Any]] = None,
    active_position: Optional[Mapping[str, Any]] = None,
    signal: Any = None,
    contextual_signals: Optional[Mapping[str, Any]] = None,
) -> Optional[DogeLiveManagementSnapshot]:
    normalized_symbol = str(symbol or "").strip().upper()
    if not normalized_symbol:
        return None

    resolved_overview = overview or executor.fetch_account_overview(symbol=normalized_symbol)
    resolved_position = dict(active_position) if active_position is not None else _find_active_position(resolved_overview, normalized_symbol)
    if resolved_position is None:
        return None

    resolved_signal = signal
    if resolved_signal is None:
        resolved_signal = _analyze_timeframe(
            executor,
            symbol=normalized_symbol,
            interval=timeframe,
            score_threshold=score_threshold,
        )

    resolved_contextual: dict[str, Any] = {}
    if contextual_signals is not None:
        for raw_timeframe, context_signal in contextual_signals.items():
            normalized_timeframe = str(raw_timeframe or "").strip()
            if normalized_timeframe:
                resolved_contextual[normalized_timeframe] = context_signal
    else:
        for raw_timeframe in context_timeframes:
            normalized_timeframe = str(raw_timeframe or "").strip()
            if not normalized_timeframe or normalized_timeframe == timeframe:
                continue
            resolved_contextual[normalized_timeframe] = _analyze_timeframe(
                executor,
                symbol=normalized_symbol,
                interval=normalized_timeframe,
                score_threshold=score_threshold,
            )

    approval = get_latest_trade_approval(symbol=normalized_symbol, status="consumed") or {}
    proposal = approval.get("proposal") or {}
    entry_price = _decimal(resolved_position.get("entry_price") or getattr(resolved_signal, "last_close", "0"), "0")
    market_price = _decimal(getattr(resolved_signal, "last_close", "0"), "0")
    quantity = abs(_decimal(resolved_position.get("position_amt"), "0"))
    entry_side = _entry_side_from_position(resolved_position)
    stop_loss_pct = _decimal(proposal.get("stop_loss_pct"), str(default_stop_loss_pct))
    take_profit_pct = _decimal(proposal.get("take_profit_pct"), str(default_take_profit_pct))
    plan = plan_doge_management(
        entry_side=entry_side,
        entry_price=entry_price,
        market_price=market_price,
        quantity=quantity,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        primary_signal=resolved_signal,
        context_signals=resolved_contextual,
    )

    rules = executor._get_symbol_rules(normalized_symbol)
    protective_orders = executor.get_protective_orders(normalized_symbol, entry_side=entry_side)
    protective_orders_missing = not (
        protective_orders.get("stop_loss") and protective_orders.get("take_profit")
    )
    current_stop_price = _decimal(
        protective_orders.get("stop_loss_price") or plan.original_stop_price,
        str(plan.original_stop_price),
    )
    current_take_profit_price = _decimal(
        protective_orders.get("take_profit_price") or plan.original_take_profit_price,
        str(plan.original_take_profit_price),
    )
    recommended_stop_price = executor.normalize_protective_price(
        symbol=normalized_symbol,
        entry_side=entry_side,
        purpose="stop_loss",
        price=plan.suggested_stop_price,
        rules=rules,
    )
    recommended_take_profit_price = executor.normalize_protective_price(
        symbol=normalized_symbol,
        entry_side=entry_side,
        purpose="take_profit",
        price=plan.suggested_take_profit_price,
        rules=rules,
    )

    return DogeLiveManagementSnapshot(
        symbol=normalized_symbol,
        timeframe=timeframe,
        signal=resolved_signal,
        contextual_signals=resolved_contextual,
        active_position=resolved_position,
        approval=approval,
        entry_side=entry_side,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        price_tick=rules.price_tick,
        plan=plan,
        protective_orders=protective_orders,
        protective_orders_missing=protective_orders_missing,
        current_stop_price=current_stop_price,
        current_take_profit_price=current_take_profit_price,
        recommended_stop_price=recommended_stop_price,
        recommended_take_profit_price=recommended_take_profit_price,
    )