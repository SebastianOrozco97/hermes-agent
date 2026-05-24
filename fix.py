with open('../hermes_home/scripts/doge_live_scout.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('''        positions = get_open_positions()
    if positions:''', '''    positions = get_open_positions()
    if positions:''')

with open('../hermes_home/scripts/doge_live_scout.py', 'w', encoding='utf-8') as f:
    f.write(text)
