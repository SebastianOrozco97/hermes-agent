import re

file_path = "tools/doge_premium_gemini_verifier.py"
with open(file_path, 'r', encoding='utf-8') as f:
    content = f.read()

# Inject macro instruction
old_sys_prompt = """"Eres el analizador supremo para entradas DOGEUSDT supervisadas por humano. "
        "Recibes solo metricas deterministas y la validacion previa de Gemini 3.1 Flash Lite. "
        "No inventes noticias ni datos externos. Devuelve solo JSON valido sin markdown con estas keys exactas: "
        "recommended_action debe ser approve_entry o reject_entry. risk_label debe ser normal o alto_riesgo. "
        "confidence va de 0 a 1. Si hay dudas, pass_trade=false y recommended_action=reject_entry."""

new_sys_prompt = """"Eres el analizador supremo para entradas DOGEUSDT supervisadas por humano. "
        "Recibes solo metricas deterministas y la validacion previa de Gemini 3.1 Flash Lite. "
        "ATENCION: Evalua la variable 'macro_state' (si existe). Si el alineamiento Macro es Opuesto al 'side', debes castigar severamente el confidence o rechazar. "
        "No inventes noticias ni datos externos. Devuelve solo JSON valido sin markdown con estas keys exactas: "
        "recommended_action debe ser approve_entry o reject_entry. risk_label debe ser normal o alto_riesgo. "
        "confidence va de 0 a 1. Si hay dudas, pass_trade=false y recommended_action=reject_entry."""

content = content.replace(old_sys_prompt, new_sys_prompt)

with open(file_path, 'w', encoding='utf-8') as f:
    f.write(content)
print("Updated doge_premium_gemini_verifier system prompt.")
