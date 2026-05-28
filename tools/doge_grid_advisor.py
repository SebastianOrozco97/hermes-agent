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
    market_price: Decimal
    levels: List[GridLevel]
    total_required_capital: Decimal
    stop_loss_price_lower: Decimal
    stop_loss_price_upper: Decimal
    leverage: Decimal
    regime: str
    regime_reason: str
    regime_allows_entry: bool
    rationale: str


def _classify_grid_regime(
    *,
    market_price: Decimal,
    atr: Decimal,
    trend_bias_pct: Decimal | None,
    atr_ratio_limit: Decimal,
    trend_bias_limit: Decimal,
) -> tuple[bool, str, str]:
    atr_ratio = abs(atr) / market_price if market_price > 0 else Decimal("0")
    normalized_trend_bias = abs(trend_bias_pct or Decimal("0"))
    if atr_ratio > atr_ratio_limit:
        return False, "high_volatility", f"ATR/price {atr_ratio:.4f} exceeds {atr_ratio_limit:.4f}"
    if normalized_trend_bias > trend_bias_limit:
        return False, "trend", f"trend bias {normalized_trend_bias:.4f} exceeds {trend_bias_limit:.4f}"
    return True, "range_bound", f"ATR/price {atr_ratio:.4f} and trend bias {normalized_trend_bias:.4f} stay inside range limits"

def plan_dynamic_grid(
    symbol: str, 
    market_price: Decimal, 
    atr: Decimal, 
    available_capital: Decimal, 
    grids_per_side: int = 5,
    atr_multiplier: Decimal = Decimal("2.0"),
    trend_bias_pct: Decimal | None = None,
    leverage: Decimal = Decimal("1"),
    atr_ratio_limit: Decimal = Decimal("0.08"),
    trend_bias_limit: Decimal = Decimal("0.015"),
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
    regime_allows_entry, regime, regime_reason = _classify_grid_regime(
        market_price=market_price,
        atr=atr,
        trend_bias_pct=trend_bias_pct,
        atr_ratio_limit=atr_ratio_limit,
        trend_bias_limit=trend_bias_limit,
    )
    
    rationale = (
        f"Grid calculated with ATR {atr}. Range: {lower_bound} to {upper_bound}. "
        f"Spacing: {grid_spacing}. Regime: {regime} ({regime_reason})."
    )
    
    return GridPlan(
        symbol=symbol,
        market_price=market_price,
        levels=levels,
        total_required_capital=available_capital,
        stop_loss_price_lower=sl_lower,
        stop_loss_price_upper=sl_upper,
        leverage=leverage,
        regime=regime,
        regime_reason=regime_reason,
        regime_allows_entry=regime_allows_entry,
        rationale=rationale
    )
