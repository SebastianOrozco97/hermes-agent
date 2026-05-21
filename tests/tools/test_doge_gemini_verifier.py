from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

import tools.doge_gemini_verifier as verifier


def _mock_response(content: str):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=content),
            )
        ]
    )


def _snapshots() -> dict[str, dict[str, str | int]]:
    return {
        "15m": {
            "symbol": "DOGEUSDT",
            "timeframe": "15m",
            "last_close": "0.1036",
            "ema_fast": "0.1031",
            "ema_slow": "0.1029",
            "rsi_14": "61.64",
            "volume_ratio": "1.18",
            "breakout_reference": "0.1034",
            "signal_score": 6,
            "verdict": "candidate_long",
        },
        "1h": {
            "symbol": "DOGEUSDT",
            "timeframe": "1h",
            "last_close": "0.1034",
            "ema_fast": "0.1028",
            "ema_slow": "0.1019",
            "rsi_14": "58.10",
            "volume_ratio": "1.02",
            "breakout_reference": "0.1032",
            "signal_score": 5,
            "verdict": "candidate_long",
        },
    }


def test_verify_doge_setup_with_gemini_parses_json(monkeypatch):
    monkeypatch.setattr(
        verifier,
        "call_llm",
        lambda **kwargs: _mock_response(
            """
            ```json
            {
              "pass_trade": true,
              "confidence": 0.82,
              "summary": "Alineacion alcista razonable entre 15m y 1h.",
              "scenario_30_90m": "Probable continuidad o consolidacion alta sobre breakout.",
              "future_bias_4_12h": "Sesgo alcista moderado mientras respete las medias.",
              "invalidation": "Perder EMA21 15m y fallar el retest del breakout.",
              "risk_flags": ["volumen 1h todavia moderado"],
              "operator_note": "Esperar confirmacion de cierre si entra tarde a la vela."
            }
            ```
            """
        ),
    )

    assessment = verifier.verify_doge_setup_with_gemini(
        symbol="DOGEUSDT",
        primary_timeframe="15m",
        timeframe_snapshots=_snapshots(),
        exchange_preview={"quantity": "50", "reference_price": "0.1036", "estimated_notional_usd": "5.18"},
        notional_usd=Decimal("5.25"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        leverage=Decimal("1"),
    )

    assert assessment.passed is True
    assert assessment.confidence == Decimal("0.82")
    assert assessment.model == "gemini-3.1-flash-lite"
    assert assessment.risk_flags == ("volumen 1h todavia moderado",)


def test_verify_doge_setup_with_gemini_rejects_invalid_json(monkeypatch):
    monkeypatch.setattr(verifier, "call_llm", lambda **kwargs: _mock_response("sin json"))

    with pytest.raises(verifier.DogeGeminiVerifierError):
        verifier.verify_doge_setup_with_gemini(
            symbol="DOGEUSDT",
            primary_timeframe="15m",
            timeframe_snapshots=_snapshots(),
            exchange_preview={"quantity": "50", "reference_price": "0.1036", "estimated_notional_usd": "5.18"},
            notional_usd=Decimal("5.25"),
            stop_loss_pct=Decimal("0.5"),
            take_profit_pct=Decimal("1.0"),
            leverage=Decimal("1"),
        )