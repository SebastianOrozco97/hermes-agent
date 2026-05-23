import re

file_path = "tools/binance_guardrails.py"
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Add macro_alignment to BinanceTradeProposal
old_class_sig = """class BinanceTradeProposal:
    symbol: str
    side: str
    notional_usd: Decimal
    mode: str = "paper"
    order_type: str = "MARKET"
    stop_loss_pct: Optional[Decimal] = None
    take_profit_pct: Optional[Decimal] = None
    leverage: Decimal = Decimal("1")
    rationale: str = ""
    verifier_model: Optional[str] = None
    verifier_passed: bool = False
    verifier_confidence: Optional[Decimal] = None
    dry_run: bool = True"""

new_class_sig = """class BinanceTradeProposal:
    symbol: str
    side: str
    notional_usd: Decimal
    mode: str = "paper"
    order_type: str = "MARKET"
    stop_loss_pct: Optional[Decimal] = None
    take_profit_pct: Optional[Decimal] = None
    leverage: Decimal = Decimal("1")
    rationale: str = ""
    verifier_model: Optional[str] = None
    verifier_passed: bool = False
    verifier_confidence: Optional[Decimal] = None
    dry_run: bool = True
    macro_alignment: str = "aligned" """

content = content.replace(old_class_sig, new_class_sig)

old_payload = """dry_run=_parse_bool(payload.get("dry_run"), default=True),"""
new_payload = """dry_run=_parse_bool(payload.get("dry_run"), default=True),
            macro_alignment=str(payload.get("macro_alignment", "aligned")).strip().lower(),"""
content = content.replace(old_payload, new_payload)

old_dict = """"dry_run": self.dry_run,"""
new_dict = """"dry_run": self.dry_run,
            "macro_alignment": self.macro_alignment,"""
content = content.replace(old_dict, new_dict)

# Add custom rule to evaluate_trade_proposal
old_eval = """if proposal.notional_usd > limits.max_notional_usd:
        reasons.append(
            f"notional_usd {proposal.notional_usd} exceeds max_notional_usd {limits.max_notional_usd}"
        )"""

new_eval = """# Dynamic macro sizing check
    effective_max_notional = limits.max_notional_usd
    if proposal.macro_alignment == "divergent":
        effective_max_notional = limits.max_notional_usd * Decimal("0.5")  # Risk Slash 50%
        
    if proposal.notional_usd > effective_max_notional:
        reasons.append(
            f"notional_usd {proposal.notional_usd} exceeds effective_max_notional {effective_max_notional} (Macro Alignment: {proposal.macro_alignment})"
        )"""
content = content.replace(old_eval, new_eval)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated guardrails with dynamic sizing.")
