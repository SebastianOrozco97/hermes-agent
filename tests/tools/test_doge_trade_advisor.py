from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from tools.doge_trade_advisor import plan_doge_long_management


def _signal(
    *,
    last_close: str,
    ema_fast: str,
    ema_slow: str,
    breakout: str,
    score: int,
    verdict: str,
    volume_ratio: str = "1.10",
):
    return SimpleNamespace(
        last_close=Decimal(last_close),
        ema_fast=Decimal(ema_fast),
        ema_slow=Decimal(ema_slow),
        breakout_reference=Decimal(breakout),
        signal_score=score,
        verdict=verdict,
        volume_ratio=Decimal(volume_ratio),
    )


def test_plan_doge_long_management_moves_stop_to_break_even_on_clean_progress():
    plan = plan_doge_long_management(
        entry_price=Decimal("0.100000"),
        market_price=Decimal("0.100500"),
        quantity=Decimal("50"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        primary_signal=_signal(
            last_close="0.100500",
            ema_fast="0.100350",
            ema_slow="0.100150",
            breakout="0.100400",
            score=6,
            verdict="candidate_long",
            volume_ratio="1.18",
        ),
        context_signals={
            "1h": _signal(
                last_close="0.100700",
                ema_fast="0.100200",
                ema_slow="0.099900",
                breakout="0.100300",
                score=5,
                verdict="candidate_long",
            )
        },
    )

    assert plan.action == "raise_stop_breakeven"
    assert plan.suggested_stop_price == Decimal("0.100000")
    assert plan.progress_to_take_profit == Decimal("0.5")


def test_plan_doge_long_management_trails_and_extends_when_structure_stays_strong():
    plan = plan_doge_long_management(
        entry_price=Decimal("0.100000"),
        market_price=Decimal("0.100900"),
        quantity=Decimal("50"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        primary_signal=_signal(
            last_close="0.100900",
            ema_fast="0.100600",
            ema_slow="0.100250",
            breakout="0.100650",
            score=6,
            verdict="candidate_long",
            volume_ratio="1.22",
        ),
        context_signals={
            "1h": _signal(
                last_close="0.101100",
                ema_fast="0.100500",
                ema_slow="0.100100",
                breakout="0.100700",
                score=5,
                verdict="candidate_long",
            ),
            "4h": _signal(
                last_close="0.101500",
                ema_fast="0.100300",
                ema_slow="0.099800",
                breakout="0.100900",
                score=5,
                verdict="candidate_long",
            ),
        },
    )

    assert plan.action == "trail_and_extend"
    assert plan.suggested_stop_price > Decimal("0.100000")
    assert plan.suggested_take_profit_price > Decimal("0.101000")


def test_plan_doge_long_management_requests_defensive_exit_when_structure_breaks():
    plan = plan_doge_long_management(
        entry_price=Decimal("0.100000"),
        market_price=Decimal("0.099400"),
        quantity=Decimal("50"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        primary_signal=_signal(
            last_close="0.099400",
            ema_fast="0.099700",
            ema_slow="0.099850",
            breakout="0.100200",
            score=2,
            verdict="standby",
            volume_ratio="0.92",
        ),
        context_signals={
            "1h": _signal(
                last_close="0.099500",
                ema_fast="0.099900",
                ema_slow="0.100100",
                breakout="0.100300",
                score=2,
                verdict="standby",
                volume_ratio="0.95",
            )
        },
    )

    assert plan.action == "exit_defensive"
    assert "estructura" in plan.rationale.lower()


def test_plan_doge_long_management_tightens_stop_when_profit_weakens():
    plan = plan_doge_long_management(
        entry_price=Decimal("0.100000"),
        market_price=Decimal("0.100250"),
        quantity=Decimal("50"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        primary_signal=_signal(
            last_close="0.100250",
            ema_fast="0.100320",
            ema_slow="0.100180",
            breakout="0.100400",
            score=3,
            verdict="standby",
            volume_ratio="0.98",
        ),
        context_signals={
            "1h": _signal(
                last_close="0.100100",
                ema_fast="0.100250",
                ema_slow="0.100150",
                breakout="0.100350",
                score=3,
                verdict="standby",
                volume_ratio="0.99",
            )
        },
    )

    assert plan.action == "tighten_stop"
    assert plan.suggested_stop_price > Decimal("0.099500")