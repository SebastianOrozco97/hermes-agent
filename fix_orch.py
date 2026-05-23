with open('tools/execution_orchestrators.py', 'r', encoding='utf-8') as f:
    text = f.read()

text = text.replace(
    'from tools.binance_guardrails import BinanceTradeProposal, is_kill_switch_active, BinanceLiveExecutionError, BinanceRiskLimits',
    'from tools.binance_guardrails import BinanceTradeProposal, is_kill_switch_active, BinanceRiskLimits'
)

text = text.replace(
    'from tools.binance_live_adapter import BinanceFuturesLiveExecutor, BinanceSpotLiveExecutor',
    'from tools.binance_live_adapter import BinanceFuturesLiveExecutor, BinanceSpotLiveExecutor, BinanceLiveExecutionError'
)

with open('tools/execution_orchestrators.py', 'w', encoding='utf-8') as f:
    f.write(text)
