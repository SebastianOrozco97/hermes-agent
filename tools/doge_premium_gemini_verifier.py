from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Mapping, Optional, Sequence

from agent.auxiliary_client import call_llm


_DEFAULT_PREMIUM_MODEL = "gemini-3.5-flash"
_PREMIUM_MODEL_LABEL = "gemini-3.5-flash"


class DogePremiumGeminiVerifierError(RuntimeError):
    """Raised when Gemini 3.5 premium verification for DOGE is unavailable or invalid."""


@dataclass(frozen=True)
class DogePremiumGeminiAssessment:
    passed: bool
    confidence: Decimal
    summary: str
    scenario_30_90m: str
    future_bias_4_12h: str
    invalidation: str
    risk_flags: tuple[str, ...]
    operator_note: str
    recommended_action: str
    risk_label: str
    suggested_stop_price: Optional[Decimal]
    suggested_take_profit_price: Optional[Decimal]
    model: str = _PREMIUM_MODEL_LABEL

    @property
    def high_risk(self) -> bool:
        return self.risk_label == "alto_riesgo"

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "confidence": format(self.confidence.normalize(), "f"),
            "summary": self.summary,
            "scenario_30_90m": self.scenario_30_90m,
            "future_bias_4_12h": self.future_bias_4_12h,
            "invalidation": self.invalidation,
            "risk_flags": list(self.risk_flags),
            "operator_note": self.operator_note,
            "recommended_action": self.recommended_action,
            "risk_label": self.risk_label,
            "suggested_stop_price": _decimal_text(self.suggested_stop_price),
            "suggested_take_profit_price": _decimal_text(self.suggested_take_profit_price),
            "model": self.model,
        }


def _decimal_text(value: Optional[Decimal]) -> Optional[str]:
    if value is None:
        return None
    if value == value.to_integral():
        return str(value.quantize(Decimal("1")))
    return format(value.normalize(), "f")


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except Exception as exc:  # pragma: no cover - defensive adapter guard
        raise DogePremiumGeminiVerifierError("Gemini response did not include message content") from exc

    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, str):
                pieces.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    pieces.append(text.strip())
        return "\n".join(pieces).strip()
    if isinstance(content, dict):
        return json.dumps(content)
    return str(content or "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    cleaned = str(text or "").strip()
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise DogePremiumGeminiVerifierError("Gemini did not return a JSON object")

    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DogePremiumGeminiVerifierError("Gemini returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DogePremiumGeminiVerifierError("Gemini JSON payload must be an object")
    return payload


def _parse_confidence(value: Any) -> Decimal:
    try:
        confidence = Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise DogePremiumGeminiVerifierError("Gemini confidence is not a valid decimal") from exc
    if confidence > 1 and confidence <= 100:
        confidence = confidence / Decimal("100")
    if confidence < 0:
        confidence = Decimal("0")
    if confidence > 1:
        confidence = Decimal("1")
    return confidence


def _parse_risk_flags(value: Any) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, Sequence):
        items = [str(item or "").strip() for item in value]
    else:
        items = [str(value).strip()]
    return tuple(item for item in items if item)


def _parse_optional_decimal(value: Any) -> Optional[Decimal]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError) as exc:
        raise DogePremiumGeminiVerifierError("Gemini suggested price is not a valid decimal") from exc


def _risk_label(value: Any, *, fallback_high_risk: bool) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"alto_riesgo", "high_risk", "high-risk"}:
        return "alto_riesgo"
    if normalized in {"normal", "moderado", "low_risk", "low-risk", ""}:
        return "alto_riesgo" if fallback_high_risk else "normal"
    return "alto_riesgo" if fallback_high_risk else "normal"


def _call_premium_gemini(
    *,
    payload: Mapping[str, Any],
    system_prompt: str,
    model: str,
    timeout: float,
) -> DogePremiumGeminiAssessment:
    user_prompt = "Evalua el evento premium DOGE y responde solo el JSON solicitado.\n" + json.dumps(
        payload,
        ensure_ascii=True,
        indent=2,
    )

    try:
        response = call_llm(
            provider="gemini",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=520,
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - provider/network guard
        raise DogePremiumGeminiVerifierError(f"Gemini premium verification failed: {exc}") from exc

    parsed = _extract_json_object(_response_text(response))
    summary = str(parsed.get("summary") or "").strip()
    scenario_30_90m = str(parsed.get("scenario_30_90m") or "").strip()
    future_bias_4_12h = str(parsed.get("future_bias_4_12h") or "").strip()
    invalidation = str(parsed.get("invalidation") or "").strip()
    operator_note = str(parsed.get("operator_note") or "").strip()
    recommended_action = str(parsed.get("recommended_action") or "").strip().lower()

    if not summary:
        raise DogePremiumGeminiVerifierError("Gemini premium summary is required")
    if not scenario_30_90m:
        raise DogePremiumGeminiVerifierError("Gemini premium scenario_30_90m is required")
    if not future_bias_4_12h:
        raise DogePremiumGeminiVerifierError("Gemini premium future_bias_4_12h is required")
    if not invalidation:
        raise DogePremiumGeminiVerifierError("Gemini premium invalidation is required")
    if not operator_note:
        raise DogePremiumGeminiVerifierError("Gemini premium operator_note is required")
    if not recommended_action:
        raise DogePremiumGeminiVerifierError("Gemini premium recommended_action is required")

    adjustment_context = payload.get("adjustment_context") if isinstance(payload, Mapping) else None
    fallback_high_risk = bool((adjustment_context or {}).get("high_risk"))
    return DogePremiumGeminiAssessment(
        passed=bool(parsed.get("pass_trade")),
        confidence=_parse_confidence(parsed.get("confidence", "0")),
        summary=summary,
        scenario_30_90m=scenario_30_90m,
        future_bias_4_12h=future_bias_4_12h,
        invalidation=invalidation,
        risk_flags=_parse_risk_flags(parsed.get("risk_flags")),
        operator_note=operator_note,
        recommended_action=recommended_action,
        risk_label=_risk_label(parsed.get("risk_label"), fallback_high_risk=fallback_high_risk),
        suggested_stop_price=_parse_optional_decimal(parsed.get("suggested_stop_price")),
        suggested_take_profit_price=_parse_optional_decimal(parsed.get("suggested_take_profit_price")),
    )


def verify_doge_entry_with_premium_gemini(
    *,
    payload: Mapping[str, Any],
    model: str = _DEFAULT_PREMIUM_MODEL,
    timeout: float = 90.0,
) -> DogePremiumGeminiAssessment:
    system_prompt = (
        "Eres el analizador supremo para entradas DOGEUSDT supervisadas por humano. "
        "Recibes solo metricas deterministas y la validacion previa de Gemini 3.1 Flash Lite. "
        "No inventes noticias ni datos externos. Devuelve solo JSON valido sin markdown con estas keys exactas: "
        "pass_trade, confidence, summary, scenario_30_90m, future_bias_4_12h, invalidation, risk_flags, operator_note, "
        "recommended_action, risk_label, suggested_stop_price, suggested_take_profit_price. "
        "recommended_action debe ser approve_entry o reject_entry. risk_label debe ser normal o alto_riesgo. "
        "confidence va de 0 a 1. Si hay dudas, pass_trade=false y recommended_action=reject_entry."
    )
    return _call_premium_gemini(payload=payload, system_prompt=system_prompt, model=model, timeout=timeout)


def verify_doge_adjustment_with_premium_gemini(
    *,
    payload: Mapping[str, Any],
    model: str = _DEFAULT_PREMIUM_MODEL,
    timeout: float = 90.0,
) -> DogePremiumGeminiAssessment:
    system_prompt = (
        "Eres el analizador supremo para ajustes de proteccion DOGEUSDT ya abiertos y supervisados por humano. "
        "Recibes solo metricas deterministas, niveles actuales, niveles sugeridos y el contexto previo de Gemini 3.1 Flash Lite. "
        "No inventes noticias ni datos externos. Devuelve solo JSON valido sin markdown con estas keys exactas: "
        "pass_trade, confidence, summary, scenario_30_90m, future_bias_4_12h, invalidation, risk_flags, operator_note, "
        "recommended_action, risk_label, suggested_stop_price, suggested_take_profit_price. "
        "recommended_action debe ser approve_adjustment, reject_adjustment o hold_current_levels. "
        "risk_label debe ser normal o alto_riesgo. Marca alto_riesgo cuando el ajuste amplie el riesgo real. "
        "Si hay dudas, pass_trade=false y recommended_action=reject_adjustment."
    )
    return _call_premium_gemini(payload=payload, system_prompt=system_prompt, model=model, timeout=timeout)