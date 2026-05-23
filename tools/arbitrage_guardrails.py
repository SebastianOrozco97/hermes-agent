from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Mapping

@dataclass
class BinanceDeltaNeutralArbitrageProposal:
    symbol: str
    total_capital_usd: Decimal
    leverage: Decimal
    spot_side: str = "BUY"
    futures_side: str = "SELL"
    
    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "BinanceDeltaNeutralArbitrageProposal":
        return cls(
            symbol=str(payload.get("symbol", "DOGEUSDT")).strip().upper(),
            total_capital_usd=Decimal(str(payload.get("total_capital_usd", "0"))),
            leverage=Decimal(str(payload.get("leverage", "1")))
        )

def verify_delta_neutrality(proposal: BinanceDeltaNeutralArbitrageProposal, max_leverage: Decimal) -> list[str]:
    reasons = []
    if proposal.leverage > max_leverage:
        reasons.append(f"Leverage {proposal.leverage} exceeds maximum allowed leverage of {max_leverage}")
    if proposal.total_capital_usd <= 0:
        reasons.append("Total capital must be strictly positive.")
    if proposal.spot_side != "BUY" or proposal.futures_side != "SELL":
        reasons.append("Delta neutral base strategy must be Spot BUY and Futures SELL.")
    return reasons
