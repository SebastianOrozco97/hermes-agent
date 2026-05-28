from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

@dataclass
class BinanceDeltaNeutralArbitrageProposal:
    symbol: str
    total_capital_usd: Decimal
    leverage: Decimal
    spot_notional_usd: Decimal = Decimal("0")
    futures_notional_usd: Decimal = Decimal("0")
    max_notional_gap_pct: Decimal = Decimal("2.0")
    spot_side: str = "BUY"
    futures_side: str = "SELL"
    
    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BinanceDeltaNeutralArbitrageProposal":
        return cls(
            symbol=str(payload.get("symbol", "DOGEUSDT")).strip().upper(),
            total_capital_usd=Decimal(str(payload.get("total_capital_usd", "0"))),
            leverage=Decimal(str(payload.get("leverage", "1"))),
            spot_notional_usd=Decimal(str(payload.get("spot_notional_usd", "0"))),
            futures_notional_usd=Decimal(str(payload.get("futures_notional_usd", "0"))),
            max_notional_gap_pct=Decimal(str(payload.get("max_notional_gap_pct", "2.0"))),
        )

def verify_delta_neutrality(proposal: BinanceDeltaNeutralArbitrageProposal, max_leverage: Decimal) -> list[str]:
    reasons = []
    if proposal.leverage > max_leverage:
        reasons.append(f"Leverage {proposal.leverage} exceeds maximum allowed leverage of {max_leverage}")
    if proposal.total_capital_usd <= 0:
        reasons.append("Total capital must be strictly positive.")
    if proposal.spot_side != "BUY" or proposal.futures_side != "SELL":
        reasons.append("Delta neutral base strategy must be Spot BUY and Futures SELL.")
    if proposal.spot_notional_usd <= 0 or proposal.futures_notional_usd <= 0:
        reasons.append("Both spot_notional_usd and futures_notional_usd must be strictly positive.")
    else:
        gap_pct = (
            abs(proposal.spot_notional_usd - proposal.futures_notional_usd)
            / max(proposal.spot_notional_usd, proposal.futures_notional_usd)
            * Decimal("100")
        )
        if gap_pct > proposal.max_notional_gap_pct:
            reasons.append(
                f"Notional gap {gap_pct:.2f}% exceeds allowed delta-neutral gap of {proposal.max_notional_gap_pct:.2f}%"
            )
    return reasons
