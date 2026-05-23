import re

file_path = "tools/doge_premium_flow.py"
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add macro_state to entry payload
old_sig = """def build_doge_entry_premium_payload(
    *,
    symbol: str,
    timeframe: str,
    signal: Any,
    contextual_signals: Mapping[str, Any],
    exchange_preview: Mapping[str, Any],
    notional_usd: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    leverage: Decimal,
    base_rationale: str,
    market_summary: str,
    gemini_lite_assessment: Mapping[str, Any] | None,
    proposal_payload: Mapping[str, Any],
    evidence_id: str,
) -> dict[str, Any]:"""

new_sig = """def build_doge_entry_premium_payload(
    *,
    symbol: str,
    timeframe: str,
    signal: Any,
    contextual_signals: Mapping[str, Any],
    exchange_preview: Mapping[str, Any],
    notional_usd: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    leverage: Decimal,
    base_rationale: str,
    market_summary: str,
    gemini_lite_assessment: Mapping[str, Any] | None,
    proposal_payload: Mapping[str, Any],
    evidence_id: str,
    macro_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:"""

content = content.replace(old_sig, new_sig)

content = content.replace('"evidence_id": str(evidence_id or "").strip().upper(),', '"evidence_id": str(evidence_id or "").strip().upper(),\n        "macro_state": dict(macro_state or {}),')

# Add macro_state to adjustment payload
old_sig_adj = """def build_doge_adjustment_premium_payload(snapshot: Any, *, timeframe: str) -> dict[str, Any]:"""
new_sig_adj = """def build_doge_adjustment_premium_payload(snapshot: Any, *, timeframe: str, macro_state: Mapping[str, Any] | None = None) -> dict[str, Any]:"""
content = content.replace(old_sig_adj, new_sig_adj)

content = content.replace('"high_risk_reason": high_risk_reason,', '"high_risk_reason": high_risk_reason,\n            "macro_state": dict(macro_state or {}),')

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)

print("Updated doge_premium_flow.py")
