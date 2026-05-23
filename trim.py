with open('tools/binance_live_adapter.py', 'r', encoding='utf-8') as f:
    text = f.read()

# Only keep content up to the first 'from __future__ import annotations' after the start
lines = text.split('\n')
cut_idx = -1
count = 0
for i, line in enumerate(lines):
    if line.startswith('from __future__ import annotations'):
        count += 1
        if count > 1:
            cut_idx = i
            break

if cut_idx != -1:
    lines = lines[:cut_idx]
    with open('tools/binance_live_adapter.py', 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    print(f"Trimmed file at line {cut_idx}")
else:
    print("No duplicates found")
