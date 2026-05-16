# Gemma 4 + Gemini 3.1 + Binance Flow

## Current Model Assumptions

- Primary local operator model: Gemma 4 E2B. Public web results currently show Google DeepMind and Ollama references for the E2B variant.
- Initial and final verifier: Gemini 3.1 Flash-Lite. Public Google AI and Google Cloud results currently describe it as a low-latency, cost-efficient model for high-volume agentic work.
- Messaging control plane: WhatsApp through the Hermes gateway.

The names above should be treated as deploy-time configuration, not hard-coded truth inside the execution path.

## Profiles and Trust Boundaries

Use three independent Hermes profiles or containers.

### 1. Operator

- Model: Gemma 4 E2B local.
- Purpose: understand WhatsApp commands, inspect the PC, gather market context, and orchestrate subtasks.
- Toolsets: terminal, file, web, messaging, memory, todo.
- Must not hold Binance trading keys.

### 2. Trader

- Model: Gemma 4 E2B local.
- Purpose: create a structured trade proposal and call only the guarded Binance MCP.
- Toolsets: messaging, memory, todo, guarded Binance MCP.
- Must not have unrestricted terminal, browser, or file-writing authority outside its trading workspace.

### 3. Verifier

- Model: Gemini 3.1 Flash-Lite.
- Purpose: approve or reject high-risk actions before execution and review the outcome after execution.
- Input: structured trade proposal plus compact evidence, not the full terminal history.
- Output: approved or rejected, confidence, and short rationale.

## Exact Control Flow

### Observation path

1. WhatsApp message reaches Hermes gateway.
2. Operator profile classifies the request as observation, admin, or trading.
3. Observation requests call the `crypto-market-vigilance` skill.
4. The skill runs one pass, writes the local ledger, and sends the summary back to WhatsApp.

### Trading path

1. WhatsApp message reaches Hermes gateway.
2. Operator profile classifies the request as trading and delegates to the Trader profile.
3. Trader gathers fresh read-only inputs:
- market snapshot,
- account balance,
- open positions,
- daily realized PnL,
- active risk profile,
- kill-switch state.
4. Trader builds a structured `TradeIntent` object.
5. Trader asks Gemini 3.1 Flash-Lite for the initial verification verdict.
6. If Gemini rejects, Hermes reports the rejection and stops.
7. If Gemini approves, Trader calls `binance_validate_trade` on the guarded MCP.
8. If the MCP rejects, Hermes reports the exact blocked rule and stops.
9. If the MCP approves, Hermes may call `binance_submit_trade` only in paper-first mode for now.
10. Hermes captures the execution envelope or dry-run result.
11. Hermes sends the execution result back to Gemini 3.1 Flash-Lite for final verification.
12. Hermes writes a local audit record and sends a concise WhatsApp summary.

## TradeIntent Shape

```json
{
  "symbol": "BTCUSDT",
  "side": "BUY",
  "order_type": "MARKET",
  "mode": "paper",
  "notional_usd": "125",
  "stop_loss_pct": "1.25",
  "take_profit_pct": "2.50",
  "leverage": "1",
  "rationale": "Breakout retest with volume confirmation",
  "verifier_model": "gemini-3.1-flash-lite",
  "verifier_passed": true,
  "verifier_confidence": "0.86",
  "dry_run": true
}
```

## Mandatory Risk Envelope

- Default mode is `paper`.
- Live trading is disabled until a dedicated execution adapter is reviewed.
- Symbols are allowlisted.
- Position count is capped globally and per symbol.
- Notional per trade is capped.
- Minimum free balance is preserved.
- Daily loss cap blocks further execution.
- Stop loss and take profit are mandatory.
- Gemini verification is mandatory before execution.
- Local kill switch blocks every trade regardless of model output.

## Why the Verifier Is Separate

Gemma 4 E2B should do the heavy lifting because it is local and cheap. Gemini 3.1 Flash-Lite should be used as a narrow verifier, not as the main loop. That keeps cost down and makes the verification surface auditable.

## Rollout Order

1. Observation only: vigilance skill + cronjob + WhatsApp delivery.
2. Paper trading: guarded MCP validation plus dry-run submit path.
3. Exchange read-only adapter: balances, positions, and fills.
4. Live execution adapter only after paper metrics, logs, and operator kill-switch drills are stable.