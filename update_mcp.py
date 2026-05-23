import re

file_path = "agent/transports/binance_guarded_mcp_server.py"
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

old_func = """def _build_doge_premium_request_whatsapp_message(request: dict[str, Any]) -> str:
    material_payload = request.get("material_payload") or {}
    request_kind = str(request.get("request_kind") or "").strip().lower()
    symbol = str(request.get("symbol") or "DOGEUSDT").strip().upper() or "DOGEUSDT"
    kind_label = premium_request_kind_label(request_kind)
    model_label = _premium_model_display_name(str(request.get("model") or ""))
    lines = [f"Analisis premium pendiente {symbol} | {kind_label} | {model_label}"]
    if request_kind == "entry":
        base_summary = str(request.get("material_summary") or material_payload.get("market_summary") or "").strip()
        if base_summary:
            lines.append(f"Base: {base_summary}")
    elif request_kind == "adjustment":
        adjustment_context = material_payload.get("adjustment_context") or {}
        lines.append(f"Base: {adjustment_context.get('summary') or request.get('material_summary') or 'ajuste accionable'}")
        if adjustment_context.get("high_risk"):
            lines.append(
                "Riesgo: ALTO RIESGO. " + str(adjustment_context.get("high_risk_reason") or "amplia el riesgo real")
            )
    expiry_label = _operator_timestamp(str(request.get("expires_at") or ""))
    if expiry_label != "n/d":
        lines.append(f"Expira {expiry_label}")
    lines.append("Comandos: ANALIZAR DOGE | RECHAZAR ANALISIS DOGE | ESTADO DOGE")
    return "\\n".join(lines)"""


new_func = """def _build_doge_premium_request_whatsapp_message(request: dict[str, Any]) -> str:
    material_payload = request.get("material_payload") or {}
    request_kind = str(request.get("request_kind") or "").strip().lower()
    symbol = str(request.get("symbol") or "DOGEUSDT").strip().upper() or "DOGEUSDT"
    kind_label = premium_request_kind_label(request_kind)
    model_label = _premium_model_display_name(str(request.get("model") or ""))
    
    # 4. Transformacion de Salida hacia WhatsApp - Semaforo Macro
    macro_state = material_payload.get("macro_state") or {}
    macro_semaphore = ""
    if macro_state:
        btc_trend = macro_state.get("btc_trend_1h", "neutral")
        side = "BUY"
        if request_kind == "entry":
            proposal = material_payload.get("proposal_payload") or {}
            side = str(proposal.get("side") or "BUY").strip().upper()
        if side == "BUY" and btc_trend == "bullish":
            macro_semaphore = " ?? MACRO ALINEADO (BTC impulsando)"
        elif side == "SELL" and btc_trend == "bearish":
            macro_semaphore = " ?? MACRO ALINEADO (BTC impulsando bajada)"
        elif side == "BUY" and btc_trend == "bearish":
            macro_semaphore = " ?? MACRO OPUESTO (BTC arrastre bajista)"
        elif side == "SELL" and btc_trend == "bullish":
            macro_semaphore = " ?? MACRO OPUESTO (BTC divergencia alcista)"
        else:
            macro_semaphore = " ?? MACRO NEUTRAL / LATERAL"
            
    lines = [f"Analisis premium pendiente {symbol} | {kind_label} | {model_label}{macro_semaphore}"]
    if request_kind == "entry":
        base_summary = str(request.get("material_summary") or material_payload.get("market_summary") or "").strip()
        if base_summary:
            lines.append(f"Base: {base_summary}")
    elif request_kind == "adjustment":
        adjustment_context = material_payload.get("adjustment_context") or {}
        lines.append(f"Base: {adjustment_context.get('summary') or request.get('material_summary') or 'ajuste accionable'}")
        if adjustment_context.get("high_risk"):
            lines.append(
                "Riesgo: ALTO RIESGO. " + str(adjustment_context.get("high_risk_reason") or "amplia el riesgo real")
            )
    expiry_label = _operator_timestamp(str(request.get("expires_at") or ""))
    if expiry_label != "n/d":
        lines.append(f"Expira {expiry_label}")
    lines.append("Comandos: ANALIZAR DOGE | RECHAZAR ANALISIS DOGE | ESTADO DOGE")
    return "\\n".join(lines)"""

content = content.replace(old_func, new_func)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated WhatsApp formatting for Macro Semaphore.")
