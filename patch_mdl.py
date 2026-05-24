with open('agent/transports/binance_guarded_mcp_server.py', 'r', encoding='utf-8') as f:
    text = f.read()

mdl_check = '''
        if daily_realized_pnl_usd < -3.0:
            return json.dumps({
                "status": "error",
                "reason": "MAX DAILY LOSS EXCEEDED: Cannot submit new trades today."
            })
'''
if 'MAX DAILY LOSS EXCEEDED' not in text:
    text = text.replace('def binance_submit_trade(', 'def binance_submit_trade(')
    # search for internal validation inside the def
    target = 'if float(notional_usd) <= 0.0:'
    text = text.replace(target, target + mdl_check)
    with open('agent/transports/binance_guarded_mcp_server.py', 'w', encoding='utf-8') as f:
        f.write(text)
