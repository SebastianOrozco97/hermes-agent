from dataclasses import dataclass
from decimal import Decimal
from typing import List

@dataclass(frozen=True)
class GridLevel:
    price: Decimal
    side: str
    quantity: Decimal

@dataclass(frozen=True)
class GridPlan:
    symbol: str
    levels: List[GridLevel]
    total_required_capital: Decimal
    stop_loss_price_lower: Decimal
    stop_loss_price_upper: Decimal
    rationale: str

def plan_dynamic_grid(
    symbol: str, 
    market_price: Decimal, 
    atr: Decimal, 
    available_capital: Decimal, 
    grids_per_side: int = 5,
    atr_multiplier: Decimal = Decimal("2.0")
) -> GridPlan:
    """
    Calculates a symmetric grid based on volatility (ATR).
    Allocates available capital equally across all grid levels.
    """
    if available_capital <= 0 or market_price <= 0:
        raise ValueError("Price and capital must be strictly positive")
    
    range_half = atr * atr_multiplier
    if range_half <= 0:
        range_half = market_price * Decimal("0.05") # Fallback to 5% range if no ATR
        
    upper_bound = market_price + range_half
    lower_bound = market_price - range_half
    
    grid_spacing = range_half / Decimal(str(grids_per_side))
    
    # Total grids = upper grids + lower grids = grids_per_side * 2
    capital_per_grid = available_capital / Decimal(str(grids_per_side * 2))
    
    levels = []
    
    # Sells above current price
    for i in range(1, grids_per_side + 1):
        price = market_price + (grid_spacing * Decimal(str(i)))
        quantity = capital_per_grid / price
        levels.append(GridLevel(price=price, side="SELL", quantity=quantity))
        
    # Buys below current price
    for i in range(1, grids_per_side + 1):
        price = market_price - (grid_spacing * Decimal(str(i)))
        quantity = capital_per_grid / price
        levels.append(GridLevel(price=price, side="BUY", quantity=quantity))
        
    # Protective bounds (Stop Loss points)
    sl_lower = lower_bound - grid_spacing
    sl_upper = upper_bound + grid_spacing
    
    rationale = f"Grid calculated with ATR {atr}. Range: {lower_bound} to {upper_bound}. Spacing: {grid_spacing}."
    
    return GridPlan(
        symbol=symbol,
        levels=levels,
        total_required_capital=available_capital,
        stop_loss_price_lower=sl_lower,
        stop_loss_price_upper=sl_upper,
        rationale=rationale
    )
