from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Any, Mapping, Optional, Sequence

from agent.auxiliary_client import call_llm


_DEFAULT_MODEL = "gemini-3.1-flash-lite-preview"
_VERIFIER_MODEL_LABEL = "gemini-3.1-flash-lite"


class DogeGeminiVerifierError(RuntimeError):
    """Raised when Gemini output for DOGE verification is unavailable or invalid."""


@dataclass(frozen=True)
class DogeGeminiAssessment:
    passed: bool
    confidence: Decimal
    summary: str
    scenario_30_90m: str
    future_bias_4_12h: str
    invalidation: str
    risk_flags: tuple[str, ...]
    operator_note: str
    model: str = _VERIFIER_MODEL_LABEL

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
            "model": self.model,
        }


def _response_text(response: Any) -> str:
    try:
        content = response.choices[0].message.content
    except Exception as exc:  # pragma: no cover - defensive adapter guard
        raise DogeGeminiVerifierError("Gemini response did not include message content") from exc

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
        raise DogeGeminiVerifierError("Gemini did not return a JSON object")

    try:
        payload = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise DogeGeminiVerifierError("Gemini returned invalid JSON") from exc
    if not isinstance(payload, dict):
        raise DogeGeminiVerifierError("Gemini JSON payload must be an object")
    return payload


def _parse_confidence(value: Any) -> Decimal:
    try:
        confidence = Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise DogeGeminiVerifierError("Gemini confidence is not a valid decimal") from exc
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


def _build_prompt_payload(
    *,
    symbol: str,
    primary_timeframe: str,
    timeframe_snapshots: Mapping[str, Mapping[str, Any]],
    exchange_preview: Mapping[str, Any],
    notional_usd: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    leverage: Decimal,
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "entry_style": "supervised long entry only",
        "primary_timeframe": primary_timeframe,
        "timeframe_snapshots": dict(timeframe_snapshots),
        "exchange_preview": {
            "quantity": exchange_preview.get("quantity"),
            "reference_price": exchange_preview.get("reference_price"),
            "estimated_notional_usd": exchange_preview.get("estimated_notional_usd"),
        },
        "risk_plan": {
            "notional_usd": format(notional_usd.normalize(), "f"),
            "stop_loss_pct": format(stop_loss_pct.normalize(), "f"),
            "take_profit_pct": format(take_profit_pct.normalize(), "f"),
            "leverage": format(leverage.normalize(), "f"),
        },
        "instructions": {
            "goal": "Confirm whether the deterministic DOGE long setup deserves human approval now.",
            "constraints": [
                "Use only the supplied market metrics.",
                "Be conservative.",
                "If alignment is weak or uncertain, reject the trade.",
                "Do not invent news, order flow, or catalysts outside the provided data.",
            ],
        },
    }


def verify_doge_setup_with_gemini(
    *,
    symbol: str,
    primary_timeframe: str,
    timeframe_snapshots: Mapping[str, Mapping[str, Any]],
    exchange_preview: Mapping[str, Any],
    notional_usd: Decimal,
    stop_loss_pct: Decimal,
    take_profit_pct: Decimal,
    leverage: Decimal,
    model: str = _DEFAULT_MODEL,
    timeout: float = 60.0,
) -> DogeGeminiAssessment:
    payload = _build_prompt_payload(
        symbol=symbol,
        primary_timeframe=primary_timeframe,
        timeframe_snapshots=timeframe_snapshots,
        exchange_preview=exchange_preview,
        notional_usd=notional_usd,
        stop_loss_pct=stop_loss_pct,
        take_profit_pct=take_profit_pct,
        leverage=leverage,
    )
    system_prompt = (
        "Eres un verificador prudente para entradas DOGEUSDT supervisadas por humano. "
        "Recibes solo metricas deterministas multi-timeframe. No inventes noticias ni datos externos. "
        "Devuelve solo JSON valido sin markdown con estas keys exactas: "
        "pass_trade, confidence, summary, scenario_30_90m, future_bias_4_12h, invalidation, risk_flags, operator_note. "
        "confidence debe ir de 0 a 1. Si hay dudas, pass_trade=false y confidence<=0.55."
    )
    user_prompt = "Evalua el setup DOGE y responde solo el JSON solicitado.\n" + json.dumps(payload, ensure_ascii=True, indent=2)

    try:
        response = call_llm(
            provider="gemini",
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=420,
            timeout=timeout,
        )
    except Exception as exc:  # pragma: no cover - network/provider guard
        raise DogeGeminiVerifierError(f"Gemini verification failed: {exc}") from exc

    parsed = _extract_json_object(_response_text(response))
    summary = str(parsed.get("summary") or "").strip()
    scenario_30_90m = str(parsed.get("scenario_30_90m") or "").strip()
    future_bias_4_12h = str(parsed.get("future_bias_4_12h") or "").strip()
    invalidation = str(parsed.get("invalidation") or "").strip()
    operator_note = str(parsed.get("operator_note") or "").strip()

    if not summary:
        raise DogeGeminiVerifierError("Gemini summary is required")
    if not scenario_30_90m:
        raise DogeGeminiVerifierError("Gemini scenario_30_90m is required")
    if not future_bias_4_12h:
        raise DogeGeminiVerifierError("Gemini future_bias_4_12h is required")
    if not invalidation:
        raise DogeGeminiVerifierError("Gemini invalidation is required")
    if not operator_note:
        raise DogeGeminiVerifierError("Gemini operator_note is required")

    return DogeGeminiAssessment(
        passed=bool(parsed.get("pass_trade")),
        confidence=_parse_confidence(parsed.get("confidence", "0")),
        summary=summary,
        scenario_30_90m=scenario_30_90m,
        future_bias_4_12h=future_bias_4_12h,
        invalidation=invalidation,
        risk_flags=_parse_risk_flags(parsed.get("risk_flags")),
        operator_note=operator_note,
    )