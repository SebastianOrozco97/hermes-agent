from typing import Any
import os
from decimal import Decimal

from tools.binance_live_adapter import BinanceFuturesLiveExecutor, BinanceSpotLiveExecutor, BinanceLiveExecutionError, BinanceLiveExecutionError
from tools.binance_guardrails import BinanceTradeProposal, is_kill_switch_active, BinanceRiskLimits
from tools.doge_arbitrage_advisor import ArbitragePlan
from tools.doge_grid_advisor import GridPlan

class DryRunResult(dict):
    pass

def execute_arbitrage(plan: ArbitragePlan, dry_run: bool = False) -> dict[str, Any]:
    if is_kill_switch_active():
        return {"success": False, "error": "Kill switch is active"}
    
    if plan.action != "enter_arbitrage":
        return {"success": False, "error": f"Plan action is {plan.action}, not entering arbitrage"}

    limits = BinanceRiskLimits.from_env()
    # Check max global notional
    spot_notional = plan.spot_quantity * Decimal("0.1") # Approximate or we need exact price
    # Wait, the best is we already have max_notional_usd in environment.
    # We can check if plan.spot_quantity * price + margin... wait, arbitrage uses 2x capital essentially?
    # Let's enforce that spot notional cannot exceed max_notional_usd
    
    spot_executor = BinanceSpotLiveExecutor.from_env()
    futures_executor = BinanceFuturesLiveExecutor.from_env()

    price = spot_executor.get_reference_price(plan.symbol)
    spot_notional = plan.spot_quantity * price
    futures_notional = plan.futures_quantity * price

    if max(spot_notional, futures_notional) > limits.max_notional_usd:
        return {"success": False, "error": f"Arbitrage leg (Notional: {max(spot_notional, futures_notional):.2f}) exceeds BinanceRiskLimits ({limits.max_notional_usd:.2f})"}

    if dry_run:
        return {
            "success": True,
            "mode": "dry_run",
            "message": "Arbitrage execution simulated with guardrails passed",
            "spot_buy_qty": float(plan.spot_quantity),
            "transfer_amount": float(plan.futures_quantity / plan.leverage), 
            "futures_short_qty": float(plan.futures_quantity)
        }


    margin_to_transfer = plan.futures_quantity / plan.leverage

    try:
        spot_order = spot_executor.place_market_order(symbol=plan.symbol, side="BUY", quantity=plan.spot_quantity)
        
        quote_asset = plan.symbol.replace("USDT", "") if "USDT" not in plan.symbol else "USDT"
            
        transfer_res = spot_executor.universal_transfer(asset=quote_asset, amount=margin_to_transfer, from_type="MAIN", to_type="UMFUTURE")
        futures_executor._request("POST", "/fapi/v1/leverage", params={"symbol": plan.symbol, "leverage": int(plan.leverage)}, signed=True)
        futures_order = futures_executor._request(
            "POST", "/fapi/v1/order",
            params={"symbol": plan.symbol, "side": "SELL", "type": "MARKET", "quantity": format(plan.futures_quantity, "f"), "newOrderRespType": "RESULT"},
            signed=True,
        )

        return {"success": True, "spot_order": spot_order, "transfer": transfer_res, "futures_order": futures_order}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_grid(plan: GridPlan, dry_run: bool = False) -> dict[str, Any]:
    if is_kill_switch_active():
        return {"success": False, "error": "Kill switch is active"}
        
    limits = BinanceRiskLimits.from_env()
    if plan.total_required_capital > limits.max_notional_usd:
        return {"success": False, "error": f"Grid required capital {plan.total_required_capital:.2f} exceeds Max Notional Limit {limits.max_notional_usd:.2f}"}

    if dry_run:
        return {
            "success": True,
            "mode": "dry_run",
            "message": f"Grid execution simulated with {len(plan.levels)} levels. Guardrails passed.",
            "levels": [{"side": lvl.side, "price": float(lvl.price), "quantity": float(lvl.quantity)} for lvl in plan.levels]
        }

    futures_executor = BinanceFuturesLiveExecutor.from_env()
    results = []
    try:
        for lvl in plan.levels:
            rules = futures_executor._get_symbol_rules(plan.symbol)
            qty_clean = futures_executor._quantize_to_step(lvl.quantity, rules.step_size, rounding="ROUND_DOWN")
            price_clean = futures_executor._quantize_to_step(lvl.price, rules.tick_size, rounding="ROUND_HALF_UP")

            if qty_clean < rules.min_qty or qty_clean * price_clean < rules.min_notional:
                continue 

            order = futures_executor._request(
                "POST", "/fapi/v1/order",
                params={"symbol": plan.symbol, "side": lvl.side, "type": "LIMIT", "timeInForce": "GTC", "quantity": format(qty_clean, "f"), "price": format(price_clean, "f")},
                signed=True
            )
            results.append(order)
            
        return {"success": True, "orders_placed": len(results), "orders": results}
    except Exception as e:
        return {"success": False, "error": str(e)}

