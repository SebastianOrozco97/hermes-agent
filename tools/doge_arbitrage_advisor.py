from __future__ import annotations
from dataclasses import dataclass
from decimal import Decimal

from tools.binance_guardrails import get_strategy_leverage_cap

@dataclass(frozen=True)
class ArbitragePlan:
    action: str
    symbol: str
    spot_quantity: Decimal
    futures_quantity: Decimal
    leverage: Decimal
    spot_notional_usd: Decimal
    futures_notional_usd: Decimal
    futures_margin_usd: Decimal
    delta_gap_pct: Decimal
    expected_yield_pct: Decimal
    rationale: str

def plan_delta_neutral_arbitrage(
    symbol: str,
    available_capital_usd: Decimal,
    market_price: Decimal,
    funding_rate: Decimal,
    leverage: Decimal | None = None,
    min_funding_threshold: Decimal = Decimal("0.0010") # 0.10% threshold as calibration
) -> ArbitragePlan:
    if available_capital_usd <= 0 or market_price <= 0:
        raise ValueError("Capital and market price must be positive.")

    target_leverage = leverage if leverage is not None else get_strategy_leverage_cap("arbitrage")
    if target_leverage <= 0:
        raise ValueError("Leverage must be positive.")

    max_allowed_leverage = get_strategy_leverage_cap("arbitrage")
    if target_leverage > max_allowed_leverage:
        raise ValueError(
            f"Arbitrage leverage {target_leverage} exceeds configured cap {max_allowed_leverage}."
        )
    
    # Simple allocation: 80% to Spot (Buy), 20% to Futures (Short with leverage)
    spot_capital = available_capital_usd * Decimal("0.8")
    futures_margin = available_capital_usd * Decimal("0.2")
    
    # But wait, true delta neutrality means notional sizes must exactly match.
    # We want: spot_capital = futures_margin * leverage
    # And: spot_capital + futures_margin = available_capital_usd
    # Therefore: futures_margin * leverage + futures_margin = available_capital_usd
    # futures_margin * (leverage + 1) = available_capital_usd
    
    true_futures_margin = available_capital_usd / (target_leverage + Decimal("1"))
    true_spot_capital = available_capital_usd - true_futures_margin
    
    spot_quantity = true_spot_capital / market_price
    notional_futures = true_futures_margin * target_leverage
    futures_quantity = notional_futures / market_price
    delta_gap_pct = Decimal("0")
    if max(true_spot_capital, notional_futures) > 0:
        delta_gap_pct = (
            abs(true_spot_capital - notional_futures)
            / max(true_spot_capital, notional_futures)
            * Decimal("100")
        )
        if delta_gap_pct < Decimal("0.000001"):
            delta_gap_pct = Decimal("0")
    
    action = "enter_arbitrage" if funding_rate >= min_funding_threshold else "hold"
    rationale = (
        f"Funding rate is {funding_rate}. Spot notional: {true_spot_capital}, "
        f"Futures notional: {notional_futures}, Futures margin: {true_futures_margin}."
    )
    
    return ArbitragePlan(
        action=action,
        symbol=symbol,
        spot_quantity=spot_quantity,
        futures_quantity=futures_quantity,
        leverage=target_leverage,
        spot_notional_usd=true_spot_capital,
        futures_notional_usd=notional_futures,
        futures_margin_usd=true_futures_margin,
        delta_gap_pct=delta_gap_pct,
        expected_yield_pct=funding_rate * Decimal("100"),
        rationale=rationale
    )
