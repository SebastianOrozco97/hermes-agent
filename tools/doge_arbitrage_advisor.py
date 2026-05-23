from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

@dataclass(frozen=True)
class ArbitragePlan:
    action: str
    symbol: str
    spot_quantity: Decimal
    futures_quantity: Decimal
    leverage: Decimal
    expected_yield_pct: Decimal
    rationale: str

def plan_delta_neutral_arbitrage(
    symbol: str,
    available_capital_usd: Decimal,
    market_price: Decimal,
    funding_rate: Decimal,
    leverage: Decimal = Decimal("5"),
    min_funding_threshold: Decimal = Decimal("0.0010") # 0.10% threshold as calibration
) -> ArbitragePlan:
    if available_capital_usd <= 0 or market_price <= 0:
        raise ValueError("Capital and market price must be positive.")
    
    # Simple allocation: 80% to Spot (Buy), 20% to Futures (Short with 5x leverage)
    spot_capital = available_capital_usd * Decimal("0.8")
    futures_margin = available_capital_usd * Decimal("0.2")
    
    # But wait, true delta neutrality means notional sizes must exactly match.
    # We want: spot_capital = futures_margin * leverage
    # And: spot_capital + futures_margin = available_capital_usd
    # Therefore: futures_margin * leverage + futures_margin = available_capital_usd
    # futures_margin * (leverage + 1) = available_capital_usd
    
    true_futures_margin = available_capital_usd / (leverage + Decimal("1"))
    true_spot_capital = available_capital_usd - true_futures_margin
    
    spot_quantity = true_spot_capital / market_price
    notional_futures = true_futures_margin * leverage
    futures_quantity = notional_futures / market_price
    
    action = "enter_arbitrage" if funding_rate >= min_funding_threshold else "hold"
    rationale = f"Funding rate is {funding_rate}. Spot notional: {true_spot_capital}, Futures notional: {notional_futures}."
    
    return ArbitragePlan(
        action=action,
        symbol=symbol,
        spot_quantity=spot_quantity,
        futures_quantity=futures_quantity,
        leverage=leverage,
        expected_yield_pct=funding_rate * Decimal("100"),
        rationale=rationale
    )
