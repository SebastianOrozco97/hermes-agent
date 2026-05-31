from typing import Any
import hashlib
import json
import os
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from hermes_constants import get_hermes_home
from tools.arbitrage_guardrails import (
    BinanceDeltaNeutralArbitrageProposal,
    verify_delta_neutrality,
)
from tools.binance_live_adapter import (
    BinanceFuturesLiveExecutor,
    BinanceLiveExecutionError,
    BinanceSpotLiveExecutor,
    _format_decimal,
)
from tools.binance_guardrails import (
    BinanceRiskLimits,
    get_strategy_leverage_cap,
    is_kill_switch_active,
)
from tools.doge_arbitrage_advisor import ArbitragePlan
from tools.doge_grid_advisor import GridPlan

class DryRunResult(dict):
    pass


_ARBITRAGE_STATE_FILE = "binance_arbitrage_executions.json"
_GRID_STATE_FILE = "binance_grid_executions.json"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _arbitrage_state_path() -> Path:
    path = get_hermes_home() / "cache" / _ARBITRAGE_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _grid_state_path() -> Path:
    path = get_hermes_home() / "cache" / _GRID_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _load_arbitrage_states() -> dict[str, Any]:
    path = _arbitrage_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_arbitrage_states(states: dict[str, Any]) -> None:
    path = _arbitrage_state_path()
    path.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")


def _load_grid_states() -> dict[str, Any]:
    path = _grid_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _save_grid_states(states: dict[str, Any]) -> None:
    path = _grid_state_path()
    path.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")


def _record_arbitrage_state(execution_id: str, **fields: Any) -> dict[str, Any]:
    states = _load_arbitrage_states()
    record = dict(states.get(execution_id) or {})
    record.update(fields)
    record.setdefault("execution_id", execution_id)
    record["updated_at"] = _utc_now_iso()
    states[execution_id] = record
    _save_arbitrage_states(states)
    return record


def _record_grid_state(execution_id: str, **fields: Any) -> dict[str, Any]:
    states = _load_grid_states()
    record = dict(states.get(execution_id) or {})
    record.update(fields)
    record.setdefault("execution_id", execution_id)
    record["updated_at"] = _utc_now_iso()
    states[execution_id] = record
    _save_grid_states(states)
    return record


def _build_arbitrage_execution_id(plan: ArbitragePlan) -> str:
    payload = {
        "symbol": plan.symbol,
        "spot_quantity": _format_decimal(plan.spot_quantity),
        "futures_quantity": _format_decimal(plan.futures_quantity),
        "spot_notional_usd": _format_decimal(plan.spot_notional_usd),
        "futures_notional_usd": _format_decimal(plan.futures_notional_usd),
        "futures_margin_usd": _format_decimal(plan.futures_margin_usd),
        "leverage": _format_decimal(plan.leverage),
        "expected_yield_pct": _format_decimal(plan.expected_yield_pct),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16].upper()
    return f"ARB-{digest}"


def _build_grid_execution_id(plan: GridPlan) -> str:
    payload = {
        "symbol": plan.symbol,
        "market_price": _format_decimal(plan.market_price),
        "leverage": _format_decimal(plan.leverage),
        "levels": [
            {
                "side": level.side,
                "price": _format_decimal(level.price),
                "quantity": _format_decimal(level.quantity),
            }
            for level in plan.levels
        ],
        "stop_loss_price_lower": _format_decimal(plan.stop_loss_price_lower),
        "stop_loss_price_upper": _format_decimal(plan.stop_loss_price_upper),
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16].upper()
    return f"GRID-{digest}"


def _quote_asset(symbol: str) -> str:
    normalized_symbol = str(symbol or "").strip().upper()
    if normalized_symbol.endswith("USDT"):
        return "USDT"
    raise BinanceLiveExecutionError(
        f"Unsupported arbitrage quote asset resolution for symbol {normalized_symbol}"
    )


def _compensate_arbitrage(
    *,
    spot_executor: BinanceSpotLiveExecutor,
    symbol: str,
    quantity: Decimal,
    margin_transferred: Decimal,
    transfer_completed: bool,
    spot_completed: bool,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    errors: list[str] = []
    if transfer_completed and margin_transferred > 0:
        try:
            transfer_back = spot_executor.universal_transfer(
                asset=_quote_asset(symbol),
                amount=margin_transferred,
                from_type="UMFUTURE",
                to_type="MAIN",
            )
            actions.append({"step": "transfer_back", "result": transfer_back})
        except Exception as exc:  # pragma: no cover - exercised in tests via state result
            errors.append(f"transfer_back failed: {exc}")
    if spot_completed and quantity > 0:
        try:
            unwind = spot_executor.place_market_order(
                symbol=symbol,
                side="SELL",
                quantity=quantity,
            )
            actions.append({"step": "spot_unwind", "result": unwind})
        except Exception as exc:  # pragma: no cover - exercised in tests via state result
            errors.append(f"spot_unwind failed: {exc}")
    return {
        "attempted": bool(actions or errors),
        "success": not errors,
        "actions": actions,
        "errors": errors,
    }

def execute_arbitrage(plan: ArbitragePlan, dry_run: bool = False) -> dict[str, Any]:
    if is_kill_switch_active():
        return {"success": False, "error": "Kill switch is active"}
    
    if plan.action != "enter_arbitrage":
        return {"success": False, "error": f"Plan action is {plan.action}, not entering arbitrage"}

    limits = BinanceRiskLimits.from_env()
    strategy_leverage_cap = get_strategy_leverage_cap("arbitrage")
    spot_executor = BinanceSpotLiveExecutor.from_env()
    futures_executor = BinanceFuturesLiveExecutor.from_env()

    price = spot_executor.get_reference_price(plan.symbol)
    spot_notional = plan.spot_quantity * price
    futures_notional = plan.futures_quantity * price
    margin_to_transfer = futures_notional / plan.leverage
    execution_id = _build_arbitrage_execution_id(plan)

    existing_state = _load_arbitrage_states().get(execution_id)
    if isinstance(existing_state, dict):
        existing_status = str(existing_state.get("status") or "").strip().lower()
        if existing_status == "completed":
            return {
                "success": True,
                "execution_id": execution_id,
                "replayed": True,
                "execution_state": existing_state,
            }
        if existing_status in {
            "precheck_passed",
            "spot_order_placed",
            "transfer_completed",
            "needs_manual_intervention",
        }:
            return {
                "success": False,
                "error": f"Arbitrage execution {execution_id} is already in state {existing_status}",
                "execution_id": execution_id,
                "execution_state": existing_state,
            }

    if max(spot_notional, futures_notional) > limits.max_notional_usd:
        return {"success": False, "error": f"Arbitrage leg (Notional: {max(spot_notional, futures_notional):.2f}) exceeds BinanceRiskLimits ({limits.max_notional_usd:.2f})"}

    neutrality_reasons = verify_delta_neutrality(
        BinanceDeltaNeutralArbitrageProposal(
            symbol=plan.symbol,
            total_capital_usd=spot_notional + margin_to_transfer,
            leverage=plan.leverage,
            spot_notional_usd=spot_notional,
            futures_notional_usd=futures_notional,
            max_notional_gap_pct=Decimal("2.0"),
        ),
        strategy_leverage_cap,
    )
    if neutrality_reasons:
        return {
            "success": False,
            "error": "; ".join(neutrality_reasons),
            "execution_id": execution_id,
            "neutrality_reasons": neutrality_reasons,
        }

    if margin_to_transfer <= 0:
        return {
            "success": False,
            "error": "Computed futures margin transfer must be strictly positive",
            "execution_id": execution_id,
        }

    if dry_run:
        precheck_state = {
            "execution_id": execution_id,
            "status": "dry_run_preview",
            "symbol": plan.symbol,
            "leverage": _format_decimal(plan.leverage),
            "spot_quantity": _format_decimal(plan.spot_quantity),
            "futures_quantity": _format_decimal(plan.futures_quantity),
            "spot_notional_usd": _format_decimal(spot_notional),
            "futures_notional_usd": _format_decimal(futures_notional),
            "margin_to_transfer_usd": _format_decimal(margin_to_transfer),
            "created_at": _utc_now_iso(),
        }
        return {
            "success": True,
            "execution_id": execution_id,
            "mode": "dry_run",
            "message": "Arbitrage execution simulated with guardrails passed",
            "spot_buy_qty": float(plan.spot_quantity),
            "spot_notional_usd": float(spot_notional),
            "transfer_amount": float(margin_to_transfer),
            "futures_short_qty": float(plan.futures_quantity),
            "futures_notional_usd": float(futures_notional),
            "execution_state": precheck_state,
        }

    precheck_state = _record_arbitrage_state(
        execution_id,
        status="precheck_passed",
        symbol=plan.symbol,
        leverage=_format_decimal(plan.leverage),
        spot_quantity=_format_decimal(plan.spot_quantity),
        futures_quantity=_format_decimal(plan.futures_quantity),
        spot_notional_usd=_format_decimal(spot_notional),
        futures_notional_usd=_format_decimal(futures_notional),
        margin_to_transfer_usd=_format_decimal(margin_to_transfer),
        created_at=_utc_now_iso(),
    )

    spot_order = None
    transfer_res = None
    futures_order = None
    transfer_completed = False
    spot_completed = False

    try:
        futures_executor.ensure_margin_type(
            plan.symbol,
            os.getenv("BINANCE_ARBITRAGE_MARGIN_TYPE", "ISOLATED"),
        )
        futures_executor.ensure_leverage(plan.symbol, int(plan.leverage))
        spot_order = spot_executor.place_market_order(symbol=plan.symbol, side="BUY", quantity=plan.spot_quantity)
        spot_completed = True
        _record_arbitrage_state(execution_id, status="spot_order_placed", spot_order=spot_order)

        transfer_res = spot_executor.universal_transfer(asset=_quote_asset(plan.symbol), amount=margin_to_transfer, from_type="MAIN", to_type="UMFUTURE")
        transfer_completed = True
        _record_arbitrage_state(execution_id, status="transfer_completed", transfer=transfer_res)
        futures_order = futures_executor._request(
            "POST", "/fapi/v1/order",
            params={"symbol": plan.symbol, "side": "SELL", "type": "MARKET", "quantity": format(plan.futures_quantity, "f"), "newOrderRespType": "RESULT"},
            signed=True,
        )
        completed_state = _record_arbitrage_state(
            execution_id,
            status="completed",
            spot_order=spot_order,
            transfer=transfer_res,
            futures_order=futures_order,
            completed_at=_utc_now_iso(),
        )

        return {
            "success": True,
            "execution_id": execution_id,
            "spot_order": spot_order,
            "transfer": transfer_res,
            "futures_order": futures_order,
            "execution_state": completed_state,
        }
    except Exception as e:
        compensation = _compensate_arbitrage(
            spot_executor=spot_executor,
            symbol=plan.symbol,
            quantity=plan.spot_quantity,
            margin_transferred=margin_to_transfer,
            transfer_completed=transfer_completed,
            spot_completed=spot_completed,
        )
        failed_state = _record_arbitrage_state(
            execution_id,
            status="compensated" if compensation.get("success") else "needs_manual_intervention",
            error=str(e),
            spot_order=spot_order,
            transfer=transfer_res,
            futures_order=futures_order,
            compensation=compensation,
        )
        return {
            "success": False,
            "execution_id": execution_id,
            "error": str(e),
            "compensation": compensation,
            "execution_state": failed_state,
        }


def _prepare_grid_seed_levels(
    plan: GridPlan,
    futures_executor: BinanceFuturesLiveExecutor,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], Any]:
    from tools.binance_live_adapter import _quantize_to_step

    rules = futures_executor._get_symbol_rules(plan.symbol)
    prepared_levels: list[dict[str, Any]] = []
    rejected_levels: list[dict[str, Any]] = []

    for index, level in enumerate(plan.levels, start=1):
        qty_clean = _quantize_to_step(level.quantity, rules.quantity_step, rounding="down")
        price_clean = _quantize_to_step(level.price, rules.price_tick, rounding="up")
        estimated_notional = qty_clean * price_clean
        normalized_level = {
            "index": index,
            "side": level.side,
            "price": _format_decimal(price_clean),
            "quantity": _format_decimal(qty_clean),
            "estimated_notional": _format_decimal(estimated_notional),
        }

        if qty_clean <= 0:
            rejected_levels.append(
                {
                    **normalized_level,
                    "reason": "quantity rounds to zero after exchange step normalization",
                }
            )
            continue

        if estimated_notional < rules.min_notional:
            rejected_levels.append(
                {
                    **normalized_level,
                    "reason": (
                        f"estimated notional {_format_decimal(estimated_notional)} is below Binance minimum "
                        f"{_format_decimal(rules.min_notional)}"
                    ),
                }
            )
            continue

        prepared_levels.append(normalized_level)

    return prepared_levels, rejected_levels, rules


def execute_grid(plan: GridPlan, dry_run: bool = False) -> dict[str, Any]:
    if is_kill_switch_active():
        return {"success": False, "error": "Kill switch is active"}

    if not plan.regime_allows_entry:
        return {
            "success": False,
            "error": f"Grid regime rejected: {plan.regime_reason}",
            "regime": plan.regime,
        }
        
    limits = BinanceRiskLimits.from_env()
    if plan.total_required_capital > limits.max_notional_usd:
        return {"success": False, "error": f"Grid required capital {plan.total_required_capital:.2f} exceeds Max Notional Limit {limits.max_notional_usd:.2f}"}

    execution_id = _build_grid_execution_id(plan)
    existing_state = _load_grid_states().get(execution_id)
    if isinstance(existing_state, dict):
        existing_status = str(existing_state.get("status") or "").strip().lower()
        if existing_status == "active":
            return {
                "success": False,
                "error": f"Grid execution {execution_id} is already active",
                "execution_id": execution_id,
                "execution_state": existing_state,
            }
        if existing_status == "stopped_breakout":
            return {
                "success": False,
                "error": f"Grid execution {execution_id} is frozen after breakout until manual redeployment",
                "execution_id": execution_id,
                "execution_state": existing_state,
            }

    futures_executor = BinanceFuturesLiveExecutor.from_env(require_credentials=not dry_run)
    prepared_levels, rejected_levels, rules = _prepare_grid_seed_levels(plan, futures_executor)

    if not prepared_levels:
        return {
            "success": False,
            "execution_id": execution_id,
            "error": "Grid deployment rejected: no deployable levels satisfy exchange minimums",
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "rejected_levels": rejected_levels,
            "exchange_rules": {
                "quantity_step": _format_decimal(rules.quantity_step),
                "price_tick": _format_decimal(rules.price_tick),
                "min_notional": _format_decimal(rules.min_notional),
            },
        }
    if rejected_levels:
        return {
            "success": False,
            "execution_id": execution_id,
            "error": "Grid deployment rejected: all planned grid levels must remain deployable after exchange normalization",
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "deployable_levels": prepared_levels,
            "rejected_levels": rejected_levels,
            "exchange_rules": {
                "quantity_step": _format_decimal(rules.quantity_step),
                "price_tick": _format_decimal(rules.price_tick),
                "min_notional": _format_decimal(rules.min_notional),
            },
        }

    if dry_run:
        return {
            "success": True,
            "execution_id": execution_id,
            "mode": "dry_run",
            "message": f"Grid execution simulated with {len(prepared_levels)} validated levels. Guardrails passed.",
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "protective_bounds": {
                "lower": float(plan.stop_loss_price_lower),
                "upper": float(plan.stop_loss_price_upper),
            },
            "levels": [
                {
                    "side": level["side"],
                    "price": float(level["price"]),
                    "quantity": float(level["quantity"]),
                }
                for level in prepared_levels
            ],
        }

    results = []
    try:
        futures_executor.ensure_margin_type(
            plan.symbol,
            os.getenv("BINANCE_GRID_MARGIN_TYPE", "ISOLATED"),
        )
        futures_executor.ensure_leverage(plan.symbol, int(plan.leverage))
        for level in prepared_levels:
            order = futures_executor._request(
                "POST", "/fapi/v1/order",
                params={
                    "symbol": plan.symbol,
                    "side": level["side"],
                    "type": "LIMIT",
                    "timeInForce": "GTC",
                    "quantity": level["quantity"],
                    "price": level["price"],
                },
                signed=True
            )
            results.append(order)

        active_state = _record_grid_state(
            execution_id,
            status="active",
            symbol=plan.symbol,
            leverage=_format_decimal(plan.leverage),
            regime=plan.regime,
            regime_reason=plan.regime_reason,
            protective_bounds={
                "lower": _format_decimal(plan.stop_loss_price_lower),
                "upper": _format_decimal(plan.stop_loss_price_upper),
            },
            order_ids=[order.get("orderId") for order in results if order.get("orderId") not in (None, "")],
            seeded_orders=results,
            reentry_blocked=False,
            created_at=_utc_now_iso(),
            last_reference_price=_format_decimal(plan.market_price),
        )

        return {
            "success": True,
            "execution_id": execution_id,
            "orders_placed": len(results),
            "orders": results,
            "regime": plan.regime,
            "regime_reason": plan.regime_reason,
            "protective_bounds": {
                "lower": _format_decimal(plan.stop_loss_price_lower),
                "upper": _format_decimal(plan.stop_loss_price_upper),
            },
            "reentry_policy": "disabled_until_regime_revalidated",
            "execution_state": active_state,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def reconcile_grid(symbol: str = "") -> dict[str, Any]:
    normalized_symbol = str(symbol or "").strip().upper()
    states = _load_grid_states()
    if not states:
        return {"success": True, "checked": 0, "reconciled": []}

    futures_executor = BinanceFuturesLiveExecutor.from_env()
    reconciled: list[dict[str, Any]] = []
    checked = 0
    for execution_id, state in states.items():
        if not isinstance(state, dict):
            continue
        grid_symbol = str(state.get("symbol") or "").strip().upper()
        if normalized_symbol and grid_symbol != normalized_symbol:
            continue

        status = str(state.get("status") or "").strip().lower()
        if status != "active":
            continue

        checked += 1
        reference_price = futures_executor.get_reference_price(grid_symbol)
        bounds = state.get("protective_bounds") or {}
        lower_bound = Decimal(str(bounds.get("lower") or "0"))
        upper_bound = Decimal(str(bounds.get("upper") or "0"))

        if lower_bound <= reference_price <= upper_bound:
            _record_grid_state(
                execution_id,
                last_reference_price=_format_decimal(reference_price),
                last_checked_at=_utc_now_iso(),
            )
            continue

        cancelled_orders: list[dict[str, Any]] = []
        cancellation_errors: list[str] = []
        for order_id in state.get("order_ids") or []:
            if order_id in (None, ""):
                continue
            try:
                cancelled_orders.append(futures_executor.cancel_order(grid_symbol, order_id))
            except Exception as exc:
                cancellation_errors.append(str(exc))

        overview = futures_executor.fetch_account_overview(symbol=grid_symbol)
        residual_position = next(
            (
                position for position in overview.get("active_positions", [])
                if str(position.get("symbol") or "").strip().upper() == grid_symbol
            ),
            None,
        )
        breakout_side = "below_lower_bound" if reference_price < lower_bound else "above_upper_bound"
        new_status = "needs_manual_intervention" if cancellation_errors else "stopped_breakout"
        updated_state = _record_grid_state(
            execution_id,
            status=new_status,
            reentry_blocked=True,
            stopped_at=_utc_now_iso(),
            breakout_reference_price=_format_decimal(reference_price),
            breakout_side=breakout_side,
            cancelled_orders=cancelled_orders,
            cancellation_errors=cancellation_errors,
            residual_position=residual_position,
            last_reference_price=_format_decimal(reference_price),
        )
        reconciled.append(
            {
                "execution_id": execution_id,
                "symbol": grid_symbol,
                "status": new_status,
                "reference_price": _format_decimal(reference_price),
                "breakout_side": breakout_side,
                "cancelled_orders": cancelled_orders,
                "cancellation_errors": cancellation_errors,
                "residual_position": residual_position,
                "execution_state": updated_state,
            }
        )

    return {
        "success": True,
        "checked": checked,
        "reconciled": reconciled,
    }

