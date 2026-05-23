with open('../hermes_home/scripts/doge_arbitrage_scout.py', 'r', encoding='utf-8') as f:
    text = f.read()

old_lines = '''            lines.append(f"Seguimiento: APROBAR ARBITRAJE {symbol.removesuffix('USDT')} | RECHAZAR")'''

new_lines = '''            lines.append(f"Para SIMULAR responde exactamente => Simula el ARBITRAJE {symbol.removesuffix('USDT')} usando capital {capital_usd:.2f} y funding {funding_rate:.5f}")
            lines.append(f"Para APROBAR (fuego real) responde => Aprobar el ARBITRAJE {symbol.removesuffix('USDT')} usando capital {capital_usd:.2f} y funding {funding_rate:.5f}")'''

text = text.replace(old_lines, new_lines)

with open('../hermes_home/scripts/doge_arbitrage_scout.py', 'w', encoding='utf-8') as f:
    f.write(text)
