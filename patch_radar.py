import json
import os

def append_open_positions_to_scout(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()
    
    if 'def get_open_positions():' not in text:
        injection = '''
import json
import os
def get_open_positions():
    state_file = os.path.join(os.path.dirname(__file__), "..", "binance-paper-state.json")
    try:
        if os.path.exists(state_file):
            with open(state_file, "r") as f:
                data = json.load(f)
                return data.get("open_positions", [])
    except Exception:
        pass
    return []

'''
        # injecting the helper function right after imports
        text = text.replace('import json', injection, 1)

        # appending the position check before the return _emit(lines) or similar return
        if 'summary_lines = [' in text:
            # For live_scout
            text = text.replace('summary_lines = [', '''positions = get_open_positions()
    if positions:
        pos_info = " | ".join([f"{p.get('symbol')} {p.get('side')} (PnL approx)" for p in positions])
        pos_msg = f"\\n🛡 Posiciones Abiertas: {len(positions)} ({pos_info})"
    else:
        pos_msg = "\\n🛡 Posiciones Abiertas: 0"
    
    summary_lines = [pos_msg + "\\n" + ''', 1)
        elif 'lines.append(' in text:
            text = text.replace('return _emit(', '''positions = get_open_positions()
    if positions:
        pos_info = " | ".join([f"{p.get('symbol')} {p.get('side')}" for p in positions])
        lines.append(f"🛡 Posiciones Abiertas: {len(positions)} ({pos_info})")
    else:
        lines.append("🛡 Posiciones Abiertas: 0")
    return _emit(''')

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(text)

append_open_positions_to_scout('../hermes_home/scripts/doge_live_scout.py')
append_open_positions_to_scout('../hermes_home/scripts/doge_grid_scout.py')
append_open_positions_to_scout('../hermes_home/scripts/doge_arbitrage_scout.py')
