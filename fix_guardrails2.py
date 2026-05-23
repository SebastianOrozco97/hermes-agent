with open('tools/binance_guardrails.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace('    macro_alignment: str = "aligned"\n@classmethod', '    macro_alignment: str = "aligned"\n\n    @classmethod')

with open('tools/binance_guardrails.py', 'w', encoding='utf-8') as f:
    f.write(text)
