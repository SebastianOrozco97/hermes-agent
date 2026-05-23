from dataclasses import dataclass
from decimal import Decimal
from typing import List, Mapping, Any
from tools.doge_grid_advisor import GridPlan, GridLevel

@dataclass
class GridManagerAction:
    action: str
    cancel_order_ids: List[str]
    new_orders: List[GridLevel]
    reason: str

def manage_active_grid(
    plan: GridPlan,
    current_price: Decimal,
    active_orders: List[Mapping[str, Any]]
) -> GridManagerAction:
    """
    Evaluates the live status of the grid. If the price goes beyond stop margins,
    it tears down the grid. Otherwise, it maintains the structure.
    (Self-healing logic for pairing filled orders goes here in production map).
    """
    if current_price <= plan.stop_loss_price_lower or current_price >= plan.stop_loss_price_upper:
        # Price broke the grid limits. Tear it down.
        order_ids_to_cancel = [str(o.get("order_id", "")) for o in active_orders if o.get("status") == "NEW"]
        return GridManagerAction(
            action="terminate_grid",
            cancel_order_ids=order_ids_to_cancel,
            new_orders=[],
            reason=f"Current price {current_price} breached grid bounds ({plan.stop_loss_price_lower} - {plan.stop_loss_price_upper}). Terminating to prevent loss accumulation."
        )
        
    return GridManagerAction(
        action="maintain",
        cancel_order_ids=[],
        new_orders=[],
        reason="Market price is smoothly oscillating within the grid boundaries."
    )
