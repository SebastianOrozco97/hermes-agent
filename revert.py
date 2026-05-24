import json
import re

def revert(filepath):
    with open(filepath, 'r', encoding='utf-8') as f:
        text = f.read()

    # remove the injected helper function using regex
    text = re.sub(r'\nimport json\nimport os\ndef get_open_positions\(\):.*?\n    return \[\]\n\n', '\nimport json', text, flags=re.DOTALL)
    
    # remove the injection strings
    patch1 = '''positions = get_open_positions()
    if positions:
        pos_info = " | ".join([f"{p.get('symbol')} {p.get('side')} (PnL approx)" for p in positions])
        pos_msg = f"\\n🛡 Posiciones Abiertas: {len(positions)} ({pos_info})"
    else:
        pos_msg = "\\n🛡 Posiciones Abiertas: 0"
    
    summary_lines = [pos_msg + "\\n" + '''
    
    patch2 = '''positions = get_open_positions()
    if positions:
        pos_info = " | ".join([f"{p.get('symbol')} {p.get('side')}" for p in positions])
        lines.append(f"🛡 Posiciones Abiertas: {len(positions)} ({pos_info})")
    else:
        lines.append("🛡 Posiciones Abiertas: 0")
    return _emit('''

    text = text.replace(patch1, 'summary_lines = [')
    text = text.replace(patch2, 'return _emit(')

    # remove malformed unicode if it was saved wrong
    text = re.sub(r'ðŸ›¡.*?\n', '', text)
    text = re.sub(r' +positions = get_open_positions\(\)\n +if positions:\n +pos_info.*?\n +lines\.append.*?\n +else:\n +lines\.append.*?\n', '', text)
    
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(text)

revert('../hermes_home/scripts/doge_live_scout.py')
