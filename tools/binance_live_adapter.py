#!/usr/bin/env python3
"""Minimal Binance USD-M Futures live adapter.

This module intentionally keeps the exchange-facing surface narrow:
- public reference price lookup
- signed account snapshot
- market entry with immediate exchange-side protective orders

The guarded MCP server remains the policy gate. This adapter only runs after
the proposal has passed local risk checks and the operator has explicitly
armed live mode via environment/config.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_UP
from typing import Any, Optional
from urllib.parse import urlencode

import hashlib
import hmac
import os
import time

import requests

from tools.binance_guardrails import BinanceAccountSnapshot, BinanceTradeProposal


_DEFAULT_BASE_URL = "https://fapi.binance.com"


class BinanceLiveExecutionError(RuntimeError):
    """Raised when the live Binance adapter cannot safely proceed."""


def _parse_decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise BinanceLiveExecutionError(f"{field_name} is not a valid decimal") from exc


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f")


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


def _quantize_to_step(value: Decimal, step: Decimal, *, rounding: str) -> Decimal:
    if step <= 0:
        return value
    mode = ROUND_UP if rounding == "up" else ROUND_DOWN
    units = (value / step).to_integral_value(rounding=mode)
    return units * step


@dataclass(frozen=True)
class SymbolTradingRules:
    quantity_step: Decimal
    price_tick: Decimal
    min_notional: Decimal


class BinanceFuturesLiveExecutor:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str = "",
        api_secret: str = "",
        timeout_s: float = 20.0,
        recv_window_ms: int = 5000,
        position_side: str = "",
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.api_secret = api_secret.strip()
        self.timeout_s = timeout_s
        self.recv_window_ms = recv_window_ms
        self.position_side = position_side.strip().upper()
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})
        if self.api_key:
            self._session.headers.update({"X-MBX-APIKEY": self.api_key})
        self._symbol_rules_cache: dict[str, SymbolTradingRules] = {}

    def _protective_retry_attempts(self) -> int:
        raw = os.getenv("BINANCE_FUTURES_PROTECTIVE_RETRY_ATTEMPTS", "3").strip() or "3"
        try:
            return max(1, min(int(raw), 6))
        except ValueError:
            return 3

    def _protective_retry_delay_s(self) -> float:
        raw = os.getenv("BINANCE_FUTURES_PROTECTIVE_RETRY_DELAY_S", "0.75").strip() or "0.75"
        try:
            return max(0.1, min(float(raw), 5.0))
        except ValueError:
            return 0.75

    @classmethod
    def from_env(cls, *, require_credentials: bool = True) -> "BinanceFuturesLiveExecutor":
        api_key = os.getenv("BINANCE_API_KEY", "").strip()
        api_secret = os.getenv("BINANCE_API_SECRET", "").strip()
        if require_credentials and (not api_key or not api_secret):
            raise BinanceLiveExecutionError(
                "Binance live adapter requires BINANCE_API_KEY and BINANCE_API_SECRET"
            )

        timeout_raw = os.getenv("BINANCE_FUTURES_TIMEOUT_S", "20").strip() or "20"
        recv_window_raw = os.getenv("BINANCE_FUTURES_RECV_WINDOW_MS", "5000").strip() or "5000"
        try:
            timeout_s = max(5.0, float(timeout_raw))
        except ValueError as exc:
            raise BinanceLiveExecutionError("BINANCE_FUTURES_TIMEOUT_S must be numeric") from exc
        try:
            recv_window_ms = max(1000, int(recv_window_raw))
        except ValueError as exc:
            raise BinanceLiveExecutionError("BINANCE_FUTURES_RECV_WINDOW_MS must be an integer") from exc

        return cls(
            base_url=os.getenv("BINANCE_FUTURES_BASE_URL", _DEFAULT_BASE_URL).strip() or _DEFAULT_BASE_URL,
            api_key=api_key,
            api_secret=api_secret,
            timeout_s=timeout_s,
            recv_window_ms=recv_window_ms,
            position_side=os.getenv("BINANCE_FUTURES_POSITION_SIDE", ""),
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        signed: bool = False,
    ) -> Any:
        payload = dict(params or {})
        if signed:
            if not self.api_key or not self.api_secret:
                raise BinanceLiveExecutionError("signed Binance request requires API credentials")
            payload.setdefault("timestamp", int(time.time() * 1000))
            payload.setdefault("recvWindow", self.recv_window_ms)
            query = urlencode(payload, doseq=True)
            payload["signature"] = hmac.new(
                self.api_secret.encode("utf-8"),
                query.encode("utf-8"),
                hashlib.sha256,
            ).hexdigest()

        url = f"{self.base_url}{path}"
        method = method.upper()
        response = self._session.request(
            method,
            url,
            params=payload if method == "GET" else None,
            data=payload if method != "GET" else None,
            timeout=self.timeout_s,
        )
        try:
            body = response.json()
        except ValueError:
            body = response.text

        if response.status_code >= 400:
            if isinstance(body, dict):
                message = body.get("msg") or body
            else:
                message = body or response.reason
            raise BinanceLiveExecutionError(
                f"Binance API {method} {path} failed: {message}"
            )

        if isinstance(body, dict) and body.get("code") not in (None, 0):
            raise BinanceLiveExecutionError(
                f"Binance API {method} {path} rejected the request: {body.get('msg') or body}"
            )
        return body

    def get_reference_price(self, symbol: str) -> Decimal:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for Binance price lookup")
        body = self._request(
            "GET",
            "/fapi/v1/premiumIndex",
            params={"symbol": normalized_symbol},
            signed=False,
        )
        mark_price = body.get("markPrice") if isinstance(body, dict) else None
        if not mark_price:
            body = self._request(
                "GET",
                "/fapi/v1/ticker/price",
                params={"symbol": normalized_symbol},
                signed=False,
            )
            mark_price = body.get("price") if isinstance(body, dict) else None
        return _parse_decimal(mark_price, field_name="reference price")

    def get_klines(self, symbol: str, *, interval: str = "15m", limit: int = 120) -> list[list[Any]]:
        normalized_symbol = str(symbol).strip().upper()
        normalized_interval = str(interval).strip() or "15m"
        normalized_limit = max(10, min(int(limit), 500))
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for Binance kline lookup")

        body = self._request(
            "GET",
            "/fapi/v1/klines",
            params={
                "symbol": normalized_symbol,
                "interval": normalized_interval,
                "limit": normalized_limit,
            },
            signed=False,
        )
        if not isinstance(body, list) or not body:
            raise BinanceLiveExecutionError(
                f"No kline data returned for {normalized_symbol} {normalized_interval}"
            )
        return body

    def _get_symbol_rules(self, symbol: str) -> SymbolTradingRules:
        normalized_symbol = str(symbol).strip().upper()
        cached = self._symbol_rules_cache.get(normalized_symbol)
        if cached is not None:
            return cached

        body = self._request(
            "GET",
            "/fapi/v1/exchangeInfo",
            params={"symbol": normalized_symbol},
            signed=False,
        )
        symbols = body.get("symbols") if isinstance(body, dict) else None
        if not symbols:
            raise BinanceLiveExecutionError(f"No exchange info available for symbol {normalized_symbol}")
        symbol_info = next(
            (item for item in symbols if str(item.get("symbol", "")).strip().upper() == normalized_symbol),
            None,
        )
        if symbol_info is None:
            raise BinanceLiveExecutionError(f"Symbol {normalized_symbol} was not found in Binance exchange info")
        quantity_step = Decimal("0")
        price_tick = Decimal("0")
        min_notional = Decimal("0")
        for item in symbol_info.get("filters", []):
            if item.get("filterType") == "MARKET_LOT_SIZE" and Decimal(str(item.get("stepSize", "0"))) > 0:
                quantity_step = Decimal(str(item.get("stepSize", "0")))
            elif item.get("filterType") == "LOT_SIZE" and quantity_step <= 0:
                quantity_step = Decimal(str(item.get("stepSize", "0")))
            elif item.get("filterType") == "PRICE_FILTER":
                price_tick = Decimal(str(item.get("tickSize", "0")))
            elif item.get("filterType") == "MIN_NOTIONAL":
                min_notional = Decimal(str(item.get("notional", item.get("minNotional", "0"))))

        if quantity_step <= 0 or price_tick <= 0:
            raise BinanceLiveExecutionError(f"Incomplete trading rules for symbol {normalized_symbol}")

        rules = SymbolTradingRules(
            quantity_step=quantity_step,
            price_tick=price_tick,
            min_notional=min_notional,
        )
        self._symbol_rules_cache[normalized_symbol] = rules
        return rules

    def _fetch_today_realized_pnl_usd(self) -> Decimal:
        utc_now = datetime.now(timezone.utc)
        start_of_day = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
        body = self._request(
            "GET",
            "/fapi/v1/income",
            params={
                "incomeType": "REALIZED_PNL",
                "startTime": int(start_of_day.timestamp() * 1000),
                "limit": 1000,
            },
            signed=True,
        )
        total = Decimal("0")
        for row in body or []:
            total += _parse_decimal(row.get("income", "0"), field_name="income")
        return total

    def fetch_account_overview(self, symbol: Optional[str] = None) -> dict[str, Any]:
        body = self._request("GET", "/fapi/v2/account", signed=True)
        normalized_symbol = str(symbol or "").strip().upper()
        positions = []
        active_positions = []
        for row in body.get("positions", []) if isinstance(body, dict) else []:
            position_amt = _parse_decimal(row.get("positionAmt", "0"), field_name="positionAmt")
            positions.append(row)
            if position_amt == 0:
                continue
            active_positions.append(
                {
                    "symbol": row.get("symbol"),
                    "position_amt": _format_decimal(position_amt),
                    "entry_price": str(row.get("entryPrice", "0")),
                    "unrealized_profit": str(row.get("unRealizedProfit", "0")),
                    "side": "LONG" if position_amt > 0 else "SHORT",
                }
            )

        positions_in_symbol = 0
        if normalized_symbol:
            positions_in_symbol = sum(
                1
                for row in positions
                if str(row.get("symbol", "")).upper() == normalized_symbol
                and _parse_decimal(row.get("positionAmt", "0"), field_name="positionAmt") != 0
            )

        snapshot = BinanceAccountSnapshot.from_payload(
            {
                "free_balance_usd": body.get("availableBalance", "0") if isinstance(body, dict) else "0",
                "open_positions": len(active_positions),
                "positions_in_symbol": positions_in_symbol,
                "daily_realized_pnl_usd": _format_decimal(self._fetch_today_realized_pnl_usd()),
                "kill_switch_active": False,
            }
        )
        return {
            "symbol": normalized_symbol or None,
            "account_snapshot": snapshot.to_dict(),
            "active_positions": active_positions,
            "account_alias": body.get("accountAlias") if isinstance(body, dict) else None,
            "asset": "USDT",
        }

    def _resolve_quantity(self, proposal: BinanceTradeProposal, price: Decimal) -> tuple[Decimal, SymbolTradingRules]:
        rules = self._get_symbol_rules(proposal.symbol)
        raw_quantity = proposal.notional_usd / price
        quantity = _quantize_to_step(raw_quantity, rules.quantity_step, rounding="down")
        if quantity <= 0:
            raise BinanceLiveExecutionError(
                f"proposal notional {proposal.notional_usd} is too small for {proposal.symbol} at price {price}"
            )
        estimated_notional = quantity * price
        if rules.min_notional > 0 and estimated_notional < rules.min_notional:
            raise BinanceLiveExecutionError(
                f"proposal resolves to { _format_decimal(estimated_notional) } USDT after quantity rounding, "
                f"below Binance minimum notional { _format_decimal(rules.min_notional) } for {proposal.symbol}"
            )
        return quantity, rules

    def preview_trade(self, proposal: BinanceTradeProposal) -> dict[str, Any]:
        reference_price = self.get_reference_price(proposal.symbol)
        quantity, rules = self._resolve_quantity(proposal, reference_price)
        estimated_notional = quantity * reference_price
        return {
            "symbol": proposal.symbol,
            "side": proposal.side,
            "quantity": _format_decimal(quantity),
            "reference_price": _format_decimal(reference_price),
            "estimated_notional_usd": _format_decimal(estimated_notional),
            "rules": {
                "quantity_step": _format_decimal(rules.quantity_step),
                "price_tick": _format_decimal(rules.price_tick),
                "min_notional": _format_decimal(rules.min_notional),
            },
        }

    def _protective_price(
        self,
        *,
        entry_price: Decimal,
        pct: Decimal,
        side: str,
        purpose: str,
        tick: Decimal,
    ) -> Decimal:
        factor = pct / Decimal("100")
        if side == "BUY":
            raw = entry_price * (Decimal("1") - factor if purpose == "stop_loss" else Decimal("1") + factor)
            rounding = "down" if purpose == "stop_loss" else "up"
        else:
            raw = entry_price * (Decimal("1") + factor if purpose == "stop_loss" else Decimal("1") - factor)
            rounding = "up" if purpose == "stop_loss" else "down"
        price = _quantize_to_step(raw, tick, rounding=rounding)
        if price <= 0:
            raise BinanceLiveExecutionError(f"computed {purpose} price is invalid: {price}")
        return price

    def _protective_rounding(self, *, entry_side: str, purpose: str) -> str:
        normalized_side = str(entry_side or "").strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise BinanceLiveExecutionError("entry_side must be BUY or SELL")
        normalized_purpose = str(purpose or "").strip().lower()
        if normalized_purpose not in {"stop_loss", "take_profit"}:
            raise BinanceLiveExecutionError("purpose must be stop_loss or take_profit")
        if normalized_side == "BUY":
            return "down" if normalized_purpose == "stop_loss" else "up"
        return "up" if normalized_purpose == "stop_loss" else "down"

    def normalize_protective_price(
        self,
        *,
        symbol: str,
        entry_side: str,
        purpose: str,
        price: Decimal,
        rules: Optional[SymbolTradingRules] = None,
    ) -> Decimal:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for protective price normalization")
        normalized_price = _parse_decimal(price, field_name=f"{purpose} price")
        resolved_rules = rules or self._get_symbol_rules(normalized_symbol)
        rounded_price = _quantize_to_step(
            normalized_price,
            resolved_rules.price_tick,
            rounding=self._protective_rounding(entry_side=entry_side, purpose=purpose),
        )
        if rounded_price <= 0:
            raise BinanceLiveExecutionError(f"normalized {purpose} price is invalid: {rounded_price}")
        return rounded_price

    def _close_side(self, entry_side: str) -> str:
        normalized_side = str(entry_side or "").strip().upper()
        if normalized_side == "BUY":
            return "SELL"
        if normalized_side == "SELL":
            return "BUY"
        raise BinanceLiveExecutionError("entry_side must be BUY or SELL")

    def _base_order_params(self) -> dict[str, str]:
        if self.position_side:
            return {"positionSide": self.position_side}
        return {}

    def _get_open_position_snapshot(self, symbol: str) -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for position lookup")
        body = self._request(
            "GET",
            "/fapi/v2/positionRisk",
            params={"symbol": normalized_symbol},
            signed=True,
        )
        positions = body if isinstance(body, list) else [body]
        for row in positions:
            if str(row.get("symbol", "") or "").strip().upper() != normalized_symbol:
                continue
            position_amt = _parse_decimal(row.get("positionAmt", "0"), field_name="positionAmt")
            if position_amt == 0:
                continue
            return {
                "symbol": normalized_symbol,
                "position_amt": _format_decimal(position_amt),
                "entry_price": str(row.get("entryPrice", "0")),
                "side": "LONG" if position_amt > 0 else "SHORT",
            }
        return {
            "symbol": normalized_symbol,
            "position_amt": "0",
            "entry_price": "0",
            "side": "FLAT",
        }

    def _place_protective_orders(
        self,
        *,
        symbol: str,
        entry_side: str,
        stop_loss_price: Decimal,
        take_profit_price: Decimal,
        rules: Optional[SymbolTradingRules] = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        resolved_rules = rules or self._get_symbol_rules(normalized_symbol)
        normalized_stop_price = self.normalize_protective_price(
            symbol=normalized_symbol,
            entry_side=entry_side,
            purpose="stop_loss",
            price=stop_loss_price,
            rules=resolved_rules,
        )
        normalized_take_profit_price = self.normalize_protective_price(
            symbol=normalized_symbol,
            entry_side=entry_side,
            purpose="take_profit",
            price=take_profit_price,
            rules=resolved_rules,
        )
        close_side = self._close_side(entry_side)

        common = {
            "symbol": normalized_symbol,
            "side": close_side,
            "closePosition": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            **self._base_order_params(),
        }
        stop_order = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                **common,
                "type": "STOP_MARKET",
                "stopPrice": _format_decimal(normalized_stop_price),
            },
            signed=True,
        )
        take_profit_order = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                **common,
                "type": "TAKE_PROFIT_MARKET",
                "stopPrice": _format_decimal(normalized_take_profit_price),
            },
            signed=True,
        )
        return {
            "stop_loss": stop_order,
            "take_profit": take_profit_order,
            "stop_loss_price": _format_decimal(normalized_stop_price),
            "take_profit_price": _format_decimal(normalized_take_profit_price),
        }

    def _arm_protective_orders(
        self,
        proposal: BinanceTradeProposal,
        *,
        entry_price: Decimal,
        rules: SymbolTradingRules,
    ) -> dict[str, Any]:
        close_side = "SELL" if proposal.side == "BUY" else "BUY"
        stop_price = self._protective_price(
            entry_price=entry_price,
            pct=proposal.stop_loss_pct or Decimal("0"),
            side=proposal.side,
            purpose="stop_loss",
            tick=rules.price_tick,
        )
        take_profit_price = self._protective_price(
            entry_price=entry_price,
            pct=proposal.take_profit_pct or Decimal("0"),
            side=proposal.side,
            purpose="take_profit",
            tick=rules.price_tick,
        )
        return self._place_protective_orders(
            symbol=proposal.symbol,
            entry_side=proposal.side,
            stop_loss_price=stop_price,
            take_profit_price=take_profit_price,
            rules=rules,
        )

    def _arm_protective_orders_with_retry(
        self,
        proposal: BinanceTradeProposal,
        *,
        entry_price: Decimal,
        rules: SymbolTradingRules,
    ) -> dict[str, Any]:
        attempts = self._protective_retry_attempts()
        delay_s = self._protective_retry_delay_s()
        errors: list[str] = []

        for attempt in range(1, attempts + 1):
            try:
                protective_orders = self._arm_protective_orders(
                    proposal,
                    entry_price=entry_price,
                    rules=rules,
                )
                protective_orders["attempts"] = attempt
                return protective_orders
            except Exception as exc:
                position_note = "position lookup unavailable"
                try:
                    snapshot = self._get_open_position_snapshot(proposal.symbol)
                    position_note = (
                        f"position_amt={snapshot.get('position_amt', '0')}"
                        f", side={snapshot.get('side', 'FLAT')}"
                        f", entry_price={snapshot.get('entry_price', '0')}"
                    )
                except Exception as snapshot_exc:
                    position_note = f"position lookup failed: {snapshot_exc}"
                errors.append(f"attempt {attempt}/{attempts}: {exc} ({position_note})")
                if attempt >= attempts:
                    break
                time.sleep(delay_s)

        raise BinanceLiveExecutionError("; ".join(errors))

    def get_protective_orders(self, symbol: str, *, entry_side: str = "") -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for open-order lookup")
        expected_close_side = self._close_side(entry_side) if str(entry_side or "").strip() else ""
        body = self._request(
            "GET",
            "/fapi/v1/openOrders",
            params={"symbol": normalized_symbol},
            signed=True,
        )
        if not isinstance(body, list):
            raise BinanceLiveExecutionError(
                f"unexpected openOrders payload for {normalized_symbol}: {type(body).__name__}"
            )

        def _order_rank(order: dict[str, Any]) -> int:
            for field in ("updateTime", "time", "orderId"):
                try:
                    return int(order.get(field) or 0)
                except (TypeError, ValueError):
                    continue
            return 0

        stop_order: Optional[dict[str, Any]] = None
        take_profit_order: Optional[dict[str, Any]] = None
        protective_orders: list[dict[str, Any]] = []
        for order in body:
            if str(order.get("symbol", "") or "").strip().upper() != normalized_symbol:
                continue
            if expected_close_side and str(order.get("side", "") or "").strip().upper() != expected_close_side:
                continue
            if str(order.get("closePosition", "") or "").strip().lower() != "true" and not _parse_bool(order.get("reduceOnly"), default=False):
                continue
            order_type = str(order.get("type", "") or "").strip().upper()
            if order_type not in {"STOP_MARKET", "TAKE_PROFIT_MARKET"}:
                continue
            protective_orders.append(order)
            if order_type == "STOP_MARKET":
                if stop_order is None or _order_rank(order) >= _order_rank(stop_order):
                    stop_order = order
            elif order_type == "TAKE_PROFIT_MARKET":
                if take_profit_order is None or _order_rank(order) >= _order_rank(take_profit_order):
                    take_profit_order = order

        return {
            "symbol": normalized_symbol,
            "entry_side": str(entry_side or "").strip().upper() or None,
            "orders": protective_orders,
            "stop_loss": stop_order,
            "take_profit": take_profit_order,
            "stop_loss_price": str((stop_order or {}).get("stopPrice") or ""),
            "take_profit_price": str((take_profit_order or {}).get("stopPrice") or ""),
        }

    def cancel_order(self, symbol: str, order_id: Any) -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required to cancel a Binance order")
        try:
            normalized_order_id = int(order_id)
        except (TypeError, ValueError) as exc:
            raise BinanceLiveExecutionError("order_id must be an integer") from exc
        return self._request(
            "DELETE",
            "/fapi/v1/order",
            params={"symbol": normalized_symbol, "orderId": normalized_order_id},
            signed=True,
        )

    def adjust_protective_orders(
        self,
        symbol: str,
        *,
        entry_side: str,
        stop_loss_price: Decimal,
        take_profit_price: Decimal,
        current_orders: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise BinanceLiveExecutionError("symbol is required for protective-order adjustment")
        normalized_entry_side = str(entry_side or "").strip().upper()
        if normalized_entry_side not in {"BUY", "SELL"}:
            raise BinanceLiveExecutionError("entry_side must be BUY or SELL")

        previous_orders = current_orders or self.get_protective_orders(normalized_symbol, entry_side=normalized_entry_side)
        rules = self._get_symbol_rules(normalized_symbol)
        new_orders = self._place_protective_orders(
            symbol=normalized_symbol,
            entry_side=normalized_entry_side,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
            rules=rules,
        )

        cancelled_orders = []
        cancellation_errors: list[str] = []
        for order in previous_orders.get("orders") or []:
            order_id = order.get("orderId")
            if order_id in (None, ""):
                continue
            try:
                cancelled_orders.append(self.cancel_order(normalized_symbol, order_id))
            except Exception as exc:
                cancellation_errors.append(str(exc))

        if cancellation_errors:
            raise BinanceLiveExecutionError(
                "new protective orders were armed, but cancelling previous protective orders failed: "
                + "; ".join(cancellation_errors)
            )

        return {
            "symbol": normalized_symbol,
            "entry_side": normalized_entry_side,
            "previous_orders": previous_orders,
            "new_orders": new_orders,
            "cancelled_orders": cancelled_orders,
        }

    def _rollback_entry(self, proposal: BinanceTradeProposal, quantity: Decimal) -> dict[str, Any]:
        rollback_side = "SELL" if proposal.side == "BUY" else "BUY"
        params = {
            "symbol": proposal.symbol,
            "side": rollback_side,
            "type": "MARKET",
            "quantity": _format_decimal(quantity),
            "reduceOnly": "true",
            **self._base_order_params(),
        }
        return self._request("POST", "/fapi/v1/order", params=params, signed=True)

    def submit_trade(self, proposal: BinanceTradeProposal) -> dict[str, Any]:
        if not _parse_bool(os.getenv("BINANCE_LIVE_TRADING_ENABLED"), default=False):
            raise BinanceLiveExecutionError("BINANCE_LIVE_TRADING_ENABLED is false")
        if proposal.mode != "live" or proposal.dry_run:
            raise BinanceLiveExecutionError("live executor only accepts mode='live' with dry_run=false")
        if proposal.order_type != "MARKET":
            raise BinanceLiveExecutionError("live Binance adapter currently supports MARKET orders only")

        leverage_int = int(proposal.leverage.to_integral_value(rounding=ROUND_DOWN))
        if Decimal(leverage_int) != proposal.leverage:
            raise BinanceLiveExecutionError("live Binance adapter requires an integer leverage value")

        reference_price = self.get_reference_price(proposal.symbol)
        quantity, rules = self._resolve_quantity(proposal, reference_price)

        leverage_result = self._request(
            "POST",
            "/fapi/v1/leverage",
            params={"symbol": proposal.symbol, "leverage": leverage_int},
            signed=True,
        )

        entry_order = self._request(
            "POST",
            "/fapi/v1/order",
            params={
                "symbol": proposal.symbol,
                "side": proposal.side,
                "type": "MARKET",
                "quantity": _format_decimal(quantity),
                "newOrderRespType": "RESULT",
                **self._base_order_params(),
            },
            signed=True,
        )

        entry_price_raw = entry_order.get("avgPrice") if isinstance(entry_order, dict) else None
        entry_price = (
            _parse_decimal(entry_price_raw, field_name="avgPrice")
            if entry_price_raw not in (None, "", "0", 0)
            else reference_price
        )

        protective_orders = None
        rollback_result = None
        try:
            protective_orders = self._arm_protective_orders_with_retry(
                proposal,
                entry_price=entry_price,
                rules=rules,
            )
        except Exception as exc:
            try:
                rollback_result = self._rollback_entry(proposal, quantity)
            except Exception as rollback_exc:
                raise BinanceLiveExecutionError(
                    "entry order executed but protective orders failed, and emergency rollback also failed: "
                    f"{rollback_exc}. original protective-order error: {exc}"
                ) from rollback_exc
            raise BinanceLiveExecutionError(
                "entry order executed but protective orders failed; emergency rollback sent successfully. "
                f"original protective-order error: {exc}"
            ) from exc

        return {
            "base_url": self.base_url,
            "quantity": _format_decimal(quantity),
            "reference_price": _format_decimal(reference_price),
            "entry_price": _format_decimal(entry_price),
            "leverage": leverage_int,
            "leverage_result": leverage_result,
            "entry_order": entry_order,
            "protective_orders": protective_orders,
            "rollback_result": rollback_result,
        }
