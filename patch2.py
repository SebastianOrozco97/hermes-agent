with open('../hermes_home/scripts/doge_live_scout.py', 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('summary_lines = [', '''positions = get_open_positions()
    if positions:
        pos_info = " | ".join([f"{p.get('symbol')} {p.get('side')} (PnL approx)" for p in positions])
        pos_msg = f"🛡 Posiciones Abiertas: {len(positions)} ({pos_info})"
    else:
        pos_msg = "🛡 Posiciones Abiertas: 0"
    
    summary_lines = [pos_msg + "\\n" + ''', 1)
with open('../hermes_home/scripts/doge_live_scout.py', 'w', encoding='utf-8') as f:
    f.write(text)
