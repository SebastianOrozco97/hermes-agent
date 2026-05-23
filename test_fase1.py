from decimal import Decimal
from tools.doge_trade_advisor import plan_doge_short_management

# Mocks para senales de mercado (Bajista)
primary_signal = {"supportive": False, "weakening": True, "last_close": Decimal("0.14"), "ema_slow": Decimal("0.15"), "score": 2}
context_signals = {
    "4h": {"supportive": False, "weakening": True, "last_close": Decimal("0.14"), "ema_slow": Decimal("0.15"), "score": 2}
}

# Escenario: Entramos en corto en .15. El precio cae a .135 (Ganando dinero a la baja).
# Stop loss inicial: 5% (0.15 * 1.05 = 0.1575)
# Take profit inicial: 10% (0.15 * 0.90 = 0.135)
plan = plan_doge_short_management(
    entry_price=Decimal("0.15"),
    market_price=Decimal("0.135"),
    quantity=Decimal("1000"),
    stop_loss_pct=Decimal("5.0"),
    take_profit_pct=Decimal("10.0"),
    primary_signal=primary_signal,
    context_signals=context_signals
)

print("--- RESULTADOS SIMULACION FASE 1 (SHORT) ---")
print(f"Buscando ganancia a la baja...")
print(f"PnL Abierto: + USD ({plan.pnl_pct}%)")
print(f"Stop Loss Original (Arriba del precio): {plan.original_stop_price}")
print(f"Take Profit Original (Abajo del precio): {plan.original_take_profit_price}")
print(f"--- ACCION RECOMENDADA POR EL ASESOR ---")
print(f"Accion: {plan.action}")
print(f"Razon: {plan.rationale}")
print(f"NUEVO Stop Loss Sugerido: {plan.suggested_stop_price}")
