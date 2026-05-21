from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Sequence


class DogeSignalError(ValueError):
    """Raised when DOGE market data is incomplete or invalid."""


def _parse_decimal(value: Any, *, field_name: str) -> Decimal:
    try:
        return Decimal(str(value).strip())
    except (AttributeError, InvalidOperation, ValueError) as exc:
        raise DogeSignalError(f"{field_name} is not a valid decimal") from exc


def _mean(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise DogeSignalError("cannot compute a mean from empty values")
    return sum(values, Decimal("0")) / Decimal(len(values))


def _clamp_decimal(value: Decimal, *, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value


@dataclass(frozen=True)
class Candle:
    open_time_ms: int
    close_time_ms: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class DogeSignalSnapshot:
    symbol: str
    timeframe: str
    last_close: Decimal
    ema_fast: Decimal
    previous_ema_fast: Decimal
    ema_slow: Decimal
    previous_ema_slow: Decimal
    rsi_14: Decimal
    volume_ratio: Decimal
    breakout_reference: Decimal
    signal_score: int
    verifier_confidence: Decimal
    verdict: str
    rationale: str
    market_summary: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "last_close": format(self.last_close.normalize(), "f"),
            "ema_fast": format(self.ema_fast.normalize(), "f"),
            "previous_ema_fast": format(self.previous_ema_fast.normalize(), "f"),
            "ema_slow": format(self.ema_slow.normalize(), "f"),
            "previous_ema_slow": format(self.previous_ema_slow.normalize(), "f"),
            "rsi_14": format(self.rsi_14.normalize(), "f"),
            "volume_ratio": format(self.volume_ratio.normalize(), "f"),
            "breakout_reference": format(self.breakout_reference.normalize(), "f"),
            "signal_score": self.signal_score,
            "verifier_confidence": format(self.verifier_confidence.normalize(), "f"),
            "verdict": self.verdict,
            "rationale": self.rationale,
            "market_summary": self.market_summary,
        }


def parse_binance_klines(rows: Sequence[Sequence[Any]]) -> list[Candle]:
    candles: list[Candle] = []
    for row in rows:
        if not isinstance(row, Sequence) or len(row) < 7:
            raise DogeSignalError("each kline row must include at least 7 fields")
        candles.append(
            Candle(
                open_time_ms=int(row[0]),
                open=_parse_decimal(row[1], field_name="open"),
                high=_parse_decimal(row[2], field_name="high"),
                low=_parse_decimal(row[3], field_name="low"),
                close=_parse_decimal(row[4], field_name="close"),
                volume=_parse_decimal(row[5], field_name="volume"),
                close_time_ms=int(row[6]),
            )
        )
    if len(candles) < 30:
        raise DogeSignalError("at least 30 closed candles are required for DOGE analysis")
    return candles


def _ema_series(values: Sequence[Decimal], period: int) -> list[Decimal]:
    if period <= 0:
        raise DogeSignalError("EMA period must be greater than zero")
    if len(values) < period:
        raise DogeSignalError(f"at least {period} values are required for EMA")

    multiplier = Decimal("2") / Decimal(period + 1)
    ema_values: list[Decimal] = [values[0]]
    previous = values[0]
    for value in values[1:]:
        previous = ((value - previous) * multiplier) + previous
        ema_values.append(previous)
    return ema_values


def _rsi(values: Sequence[Decimal], period: int) -> Decimal:
    if period <= 0:
        raise DogeSignalError("RSI period must be greater than zero")
    if len(values) <= period:
        raise DogeSignalError(f"at least {period + 1} values are required for RSI")

    deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    seed = deltas[:period]
    average_gain = sum((delta for delta in seed if delta > 0), Decimal("0")) / Decimal(period)
    average_loss = sum((-delta for delta in seed if delta < 0), Decimal("0")) / Decimal(period)

    for delta in deltas[period:]:
        gain = delta if delta > 0 else Decimal("0")
        loss = -delta if delta < 0 else Decimal("0")
        average_gain = ((average_gain * Decimal(period - 1)) + gain) / Decimal(period)
        average_loss = ((average_loss * Decimal(period - 1)) + loss) / Decimal(period)

    if average_loss == 0:
        return Decimal("100") if average_gain > 0 else Decimal("50")
    relative_strength = average_gain / average_loss
    return Decimal("100") - (Decimal("100") / (Decimal("1") + relative_strength))


def analyze_doge_15m_signal(
    candles: Sequence[Candle],
    *,
    score_threshold: int = 5,
    timeframe: str = "15m",
) -> DogeSignalSnapshot:
    if len(candles) < 40:
        raise DogeSignalError("at least 40 candles are required for DOGE signal analysis")

    closes = [candle.close for candle in candles]
    volumes = [candle.volume for candle in candles]
    ema_fast_series = _ema_series(closes, 9)
    ema_slow_series = _ema_series(closes, 21)
    rsi_14 = _rsi(closes, 14)
    average_volume = _mean(volumes[-20:])
    volume_ratio = volumes[-1] / average_volume if average_volume > 0 else Decimal("0")
    breakout_reference = max(closes[-21:-1])

    last_close = closes[-1]
    ema_fast = ema_fast_series[-1]
    previous_ema_fast = ema_fast_series[-2]
    ema_slow = ema_slow_series[-1]
    previous_ema_slow = ema_slow_series[-2]

    close_above_fast = last_close > ema_fast
    fast_above_slow = ema_fast > ema_slow
    fast_slope_up = ema_fast > previous_ema_fast
    slow_slope_up = ema_slow >= previous_ema_slow
    rsi_in_range = Decimal("52") <= rsi_14 <= Decimal("67")
    volume_confirmed = volume_ratio >= Decimal("1.10")
    breakout_confirmed = last_close >= breakout_reference

    signal_score = sum(
        int(flag)
        for flag in (
            close_above_fast,
            fast_above_slow,
            fast_slope_up,
            slow_slope_up,
            rsi_in_range,
            volume_confirmed,
            breakout_confirmed,
        )
    )
    verifier_confidence = _clamp_decimal(
        Decimal("0.46") + (Decimal(signal_score) * Decimal("0.07")),
        low=Decimal("0.10"),
        high=Decimal("0.97"),
    )
    is_candidate = (
        close_above_fast
        and fast_above_slow
        and fast_slope_up
        and slow_slope_up
        and rsi_in_range
        and signal_score >= score_threshold
    )

    verdict = "candidate_long" if is_candidate else "standby"
    rationale_parts: list[str] = []
    if close_above_fast and fast_above_slow:
        rationale_parts.append("precio sobre EMA9 y EMA21")
    if fast_slope_up and slow_slope_up:
        rationale_parts.append("pendientes de medias en ascenso")
    if rsi_in_range:
        rationale_parts.append(f"RSI14 equilibrado ({rsi_14.quantize(Decimal('0.01'))})")
    else:
        rationale_parts.append(f"RSI14 fuera de zona operable ({rsi_14.quantize(Decimal('0.01'))})")
    if volume_confirmed:
        rationale_parts.append(
            f"volumen confirma ({volume_ratio.quantize(Decimal('0.01'))}x promedio 20 velas)"
        )
    if breakout_confirmed:
        rationale_parts.append("cierre sobre el maximo reciente")

    rationale = "; ".join(rationale_parts)
    market_summary = (
        f"DOGEUSDT {timeframe}: precio {last_close.quantize(Decimal('0.000001'))}, "
        f"EMA9 {ema_fast.quantize(Decimal('0.000001'))}, EMA21 {ema_slow.quantize(Decimal('0.000001'))}, "
        f"RSI14 {rsi_14.quantize(Decimal('0.01'))}, volumen {volume_ratio.quantize(Decimal('0.01'))}x, "
        f"score {signal_score}/7."
    )
    return DogeSignalSnapshot(
        symbol="DOGEUSDT",
        timeframe=timeframe,
        last_close=last_close,
        ema_fast=ema_fast,
        previous_ema_fast=previous_ema_fast,
        ema_slow=ema_slow,
        previous_ema_slow=previous_ema_slow,
        rsi_14=rsi_14,
        volume_ratio=volume_ratio,
        breakout_reference=breakout_reference,
        signal_score=signal_score,
        verifier_confidence=verifier_confidence,
        verdict=verdict,
        rationale=rationale,
        market_summary=market_summary,
    )