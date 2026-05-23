from decimal import Decimal
from tools.doge_grid_advisor import GridPlan

def verify_grid_safety(plan: GridPlan, max_allowed_loss: Decimal, min_notional: Decimal = Decimal("5.0"), max_grid_orders: int = 20) -> list[str]:
    reasons = []
    
    # 1. Check order minimums
    for level in plan.levels:
        notional = level.price * level.quantity
        if notional < min_notional:
            reasons.append(f"Grid level at price {level.price} has notional {notional}, which is below exchange minimum of {min_notional}.")
            
    # 2. Check theoretical maximum risk exposure
    # In a grid, the worst-case scenario involves buying all lower tiers and hitting the stop loss
    # Or selling all upper tiers and hitting the stop loss upwards.
    # For now, we enforce that total capital tied up does not exceed the global daily buffer multiplied safely.
    if plan.total_required_capital > max_allowed_loss * Decimal("2"):
        reasons.append(f"Grid capital {plan.total_required_capital} exceeds the safe multiplier parameter against max daily loss {max_allowed_loss}.")
        
    # 3. Check API order limits
    if len(plan.levels) > max_grid_orders:
        reasons.append(f"Grid strategy proposes {len(plan.levels)} limit orders, which exceeds the max allowed of {max_grid_orders}.")
        
    return reasons
