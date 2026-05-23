with open('tools/binance_live_adapter.py', 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('\\"\\"\\"', '\"\"\"')
with open('tools/binance_live_adapter.py', 'w', encoding='utf-8') as f:
    f.write(text)
