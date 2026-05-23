with open('../hermes_home/scripts/doge_grid_scout.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_lines = '''        lines.append(f"Tesis: Capturar PnL sobre movimientos neutros y chop de mercado (Contraccion Volatilidad).")
        lines.append(f"Seguimiento: APROBAR GRID {symbol.removesuffix('USDT')} | RECHAZAR")'''

new_lines = '''        lines.append(f"Tesis: Capturar PnL sobre movimientos neutros y chop de mercado (Contraccion Volatilidad).")
        lines.append(f"Volatilidad (ATR): {atr_value:.5f}")
        lines.append(f"Para SIMULAR responde exactamente => Simula la GRID {symbol.removesuffix('USDT')} usando capital {plan.total_required_capital:.2f} y ATR {atr_value:.5f}")
        lines.append(f"Para APROBAR (fuego real) responde => Aprobar la GRID {symbol.removesuffix('USDT')} usando capital {plan.total_required_capital:.2f} y ATR {atr_value:.5f}")'''

text = text.replace(old_lines, new_lines)

with open('../hermes_home/scripts/doge_grid_scout.py', 'w', encoding='utf-8') as f:
    f.write(text)
