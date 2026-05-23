from decimal import Decimal
from tools.doge_arbitrage_advisor import plan_delta_neutral_arbitrage

capital_total = Decimal("1000")
precio_mercado = Decimal("0.15")
tasa_fondeo = Decimal("0.0005") # 0.05%
apalancamiento = Decimal("5")

plan = plan_delta_neutral_arbitrage(
    symbol="DOGEUSDT",
    available_capital_usd=capital_total,
    market_price=precio_mercado,
    funding_rate=tasa_fondeo,
    leverage=apalancamiento
)

spot_notional = plan.spot_quantity * precio_mercado
futures_notional = plan.futures_quantity * precio_mercado

print("--- RESULTADOS SIMULACION FASE 2 (ARBITRAJE DELTA-NEUTRAL) ---")
print(f"Capital Total Disponible:  USD")
print(f"Apalancamiento Futuros: {apalancamiento}x")
print(f"Precio DOGE: ")
print("-" * 50)
print(f"Capital asignado a SPOT (Margen real):  USD")
print(f"Capital asignado a FUTUROS (Margen real):  USD")
print("-" * 50)
print(f"Tamano nocional SPOT (Posicion Long):  USD ({plan.spot_quantity:.2f} DOGE)")
print(f"Tamano nocional FUTUROS (Posicion Short):  USD ({plan.futures_quantity:.2f} DOGE)")
print("-" * 50)
print(f"Exposicion Neta (Delta):  USD")
print(f"Yield Esperado por periodo: {plan.expected_yield_pct}%")
print(f"Accion: {plan.action}")
print(f"Justificacion: {plan.rationale}")
