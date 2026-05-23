with open('tools/binance_guardrails.py', 'r', encoding='utf-8') as f:
    text = f.read()

import re
text = re.sub(r'(\s+macro_alignment: str = "aligned"\s*){2,}', r'\n    macro_alignment: str = "aligned"\n', text)
text = re.sub(r'(\s+macro_alignment=str\(payload\.get\("macro_alignment", "aligned"\)\)\.strip\(\)\.lower\(\),\s*){2,}', r'\n            macro_alignment=str(payload.get("macro_alignment", "aligned")).strip().lower(),\n', text)
text = re.sub(r'(\s+"macro_alignment": self\.macro_alignment,\s*){2,}', r'\n            "macro_alignment": self.macro_alignment,\n', text)

with open('tools/binance_guardrails.py', 'w', encoding='utf-8') as f:
    f.write(text)
