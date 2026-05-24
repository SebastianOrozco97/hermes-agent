with open('../hermes_home/SOUL.md', 'r', encoding='utf-8') as f:
    text = f.read()

new_rule = "- Siempre que analices, propongas o rechaces una oportunidad (en cualquier fase), DEBES sugerir explícitamente valores tácticos recomendados para Stop Loss (%) y Take Profit (%) basados en tu análisis del ATR, estructura del mercado y volatilidad actual."
if new_rule not in text:
    lines = text.split('\n')
    lines.insert(25, new_rule) # Insert somewhere in the middle
    with open('../hermes_home/SOUL.md', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
