import requests
from decimal import Decimal
from typing import Any, Mapping, Optional, List

_DEFAULT_BASE_URL = "https://fapi.binance.com"

class BinanceFundingExecutionError(RuntimeError):
    pass

def fetch_premium_index(symbol: str = "DOGEUSDT") -> Mapping[str, Any]:
    try:
        response = requests.get(f"{_DEFAULT_BASE_URL}/fapi/v1/premiumIndex", params={"symbol": symbol}, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        raise BinanceFundingExecutionError(f"Failed to fetch premium index for {symbol}") from exc

def get_current_funding_rate(symbol: str = "DOGEUSDT") -> Decimal:
    data = fetch_premium_index(symbol)
    return Decimal(str(data.get("lastFundingRate", "0")))

def check_arbitrage_opportunity(symbol: str = "DOGEUSDT", min_funding_rate: Decimal = Decimal("0.0001")) -> dict:
    rate = get_current_funding_rate(symbol)
    return {
        "symbol": symbol,
        "current_funding_rate": str(rate),
        "is_opportunity": rate >= min_funding_rate,
        "required_min_rate": str(min_funding_rate),
    }

if __name__ == '__main__':
    print(check_arbitrage_opportunity())
