import re

file_path = "../hermes_home/scripts/doge_live_scout.py"
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# 1. Add fetch_btc_macro_state import
import_target = "from tools.doge_gemini_verifier import ("
new_import = "from tools.macro_data_oracle import fetch_btc_macro_state\n" + import_target
content = content.replace(import_target, new_import)

# 2. Inject fetch and update into adjustment payload
adj_target = "premium_payload = build_doge_adjustment_premium_payload("
new_adj = """macro_state = fetch_btc_macro_state().to_dict()
            premium_payload = build_doge_adjustment_premium_payload(""""
content = content.replace(adj_target, new_adj)

adj_timeframe_target = "timeframe=timeframe,"
new_adj_timeframe = "timeframe=timeframe,\n                macro_state=macro_state,"
content = content.replace(adj_timeframe_target, new_adj_timeframe)


# 3. Inject fetch and update into entry payload
entry_target = "premium_payload = build_doge_entry_premium_payload("
new_entry = """macro_state = fetch_btc_macro_state().to_dict()
    premium_payload = build_doge_entry_premium_payload(""""
content = content.replace(entry_target, new_entry)

entry_evidence_target = "evidence_id=str(evidence.get(\"evidence_id\") or \"\"),"
new_entry_evidence = "evidence_id=str(evidence.get(\"evidence_id\") or \"\"),\n        macro_state=macro_state,"
content = content.replace(entry_evidence_target, new_entry_evidence)


with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated doge_live_scout.py with Macro Oracle injection.")
