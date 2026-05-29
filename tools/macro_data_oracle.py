from dataclasses import dataclass
from typing import Mapping, Any
from enum import Enum
import requests

class MacroTrend(Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"
    NEUTRAL = "neutral"

@dataclass
class MacroState:
    btc_trend_1h: MacroTrend
    btc_trend_4h: MacroTrend
    global_funding_bias: str
    risk_level: str
    rationale: str
    
    def to_dict(self) -> dict:
        return {
            "btc_trend_1h": self.btc_trend_1h.value,
            "btc_trend_4h": self.btc_trend_4h.value,
            "global_funding_bias": self.global_funding_bias,
            "risk_level": self.risk_level,
            "rationale": self.rationale
        }


def classify_macro_alignment(state: MacroState | Mapping[str, Any]) -> str:
    if isinstance(state, MacroState):
        payload = state.to_dict()
    else:
        payload = dict(state or {})

    trend_1h = str(payload.get("btc_trend_1h", "neutral") or "neutral").strip().lower()
    trend_4h = str(payload.get("btc_trend_4h", "neutral") or "neutral").strip().lower()
    global_bias = str(payload.get("global_funding_bias", "neutral") or "neutral").strip().lower()
    risk_level = str(payload.get("risk_level", "unknown") or "unknown").strip().lower()

    if risk_level in {"unknown", "high_volatility"} and trend_4h == "bearish":
        return "blocked"
    if trend_1h == "bearish" or trend_4h == "bearish" or global_bias == "negative" or risk_level == "high_volatility":
        return "divergent"
    return "aligned"


def classify_macro_regime(state: MacroState | Mapping[str, Any]) -> str:
    if isinstance(state, MacroState):
        payload = state.to_dict()
    else:
        payload = dict(state or {})

    trend_1h = str(payload.get("btc_trend_1h", "neutral") or "neutral").strip().lower()
    trend_4h = str(payload.get("btc_trend_4h", "neutral") or "neutral").strip().lower()
    global_bias = str(payload.get("global_funding_bias", "neutral") or "neutral").strip().lower()
    risk_level = str(payload.get("risk_level", "unknown") or "unknown").strip().lower()

    if risk_level == "high_volatility":
        return "high_volatility_stress"
    if risk_level == "unknown":
        return "macro_divergent_chop"
    if trend_1h != trend_4h:
        return "macro_divergent_chop"
    if trend_4h == "bearish" or global_bias == "negative":
        return "macro_divergent_chop"
    if trend_1h == trend_4h == "bullish":
        return "supportive_macro"
    return "balanced_macro"

def fetch_btc_macro_state() -> MacroState:
    """
    Fetches real-time context about Bitcoin (the crypto macro index) 
    and general market risk state.
    (This is a simplified read-only oracle using Binance public fapi)
    """
    try:
        # We can implement a simplified check by taking the 15m or 1h klines
        # For professional readiness without complex TA-lib dependencies here,
        # we will fetch recent price action for BTCUSDT to determine simple bias.
        res = requests.get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": "BTCUSDT", "interval": "1h", "limit": 4},
            timeout=10
        )
        res.raise_for_status()
        klines = res.json()
        
        # Simple evaluation logic:
        # klines: [Open Time, Open, High, Low, Close, Volume, ...]
        first_close = float(klines[0][4])
        last_close = float(klines[-1][4])
        
        trend = MacroTrend.NEUTRAL
        if last_close > first_close * 1.002: # +0.2% in 4 hours
            trend = MacroTrend.BULLISH
        elif last_close < first_close * 0.998: # -0.2% in 4 hours
            trend = MacroTrend.BEARISH
            
        return MacroState(
            btc_trend_1h=trend,
            btc_trend_4h=trend,
            global_funding_bias="positive" if trend != MacroTrend.BEARISH else "negative",
            risk_level="high_volatility" if abs(last_close - first_close)/first_close > 0.01 else "normal",
            rationale=f"BTC moved from {first_close} to {last_close} over 4h. Trend categorized as {trend.value}."
        )
    except Exception as e:
        # Fallback if network fails
        return MacroState(
            btc_trend_1h=MacroTrend.NEUTRAL,
            btc_trend_4h=MacroTrend.NEUTRAL,
            global_funding_bias="neutral",
            risk_level="unknown",
            rationale=f"Error fetching macro state: {str(e)}"
        )
