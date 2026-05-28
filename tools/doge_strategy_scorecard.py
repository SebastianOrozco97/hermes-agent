from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from tools.binance_paper_runtime import get_paper_strategy_history_summary


_STRATEGY_LABELS = {
    "overlay_tactical_long": "Overlay tactico largo",
    "funding_arbitrage": "Arbitraje de funding",
    "atr_grid": "ATR grid",
    "no_trade": "No trade",
    "unknown": "Desconocida",
}


def _resolve_window_end_date(value: str = "") -> datetime.date:
    text = str(value or "").strip()
    if not text:
        return datetime.now(timezone.utc).date()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError as exc:
        raise ValueError("scorecard date filters must use ISO date or datetime text") from exc


def _strategy_label(strategy_id: str) -> str:
    normalized = str(strategy_id or "").strip().lower() or "unknown"
    return _STRATEGY_LABELS.get(normalized, normalized.replace("_", " ").title())


def get_doge_strategy_scorecard(
    *,
    days: int = 14,
    end_date: str = "",
    strategy_id: str = "",
    regime: str = "",
    outcome: str = "",
    home: Optional[Path] = None,
) -> dict[str, object]:
    if days <= 0:
        raise ValueError("days must be greater than zero")
    resolved_end_date = _resolve_window_end_date(end_date)
    resolved_start_date = resolved_end_date - timedelta(days=days - 1)
    summary = get_paper_strategy_history_summary(
        symbol="DOGEUSDT",
        strategy_id=strategy_id,
        regime=regime,
        outcome=outcome,
        start_date=resolved_start_date.isoformat(),
        end_date=resolved_end_date.isoformat(),
        home=home,
    )
    return {
        **summary,
        "symbol": "DOGEUSDT",
        "window_days": days,
        "start_date": resolved_start_date.isoformat(),
        "end_date": resolved_end_date.isoformat(),
    }


def get_doge_strategy_daily_scorecard(summary_date: str = "", *, home: Optional[Path] = None) -> dict[str, object]:
    return get_doge_strategy_scorecard(days=1, end_date=summary_date, home=home)


def get_doge_strategy_weekly_scorecard(end_date: str = "", *, home: Optional[Path] = None) -> dict[str, object]:
    return get_doge_strategy_scorecard(days=7, end_date=end_date, home=home)


def build_doge_strategy_scorecard_lines(scorecard: dict[str, object]) -> list[str]:
    start_date = str(scorecard.get("start_date", "") or "").strip()
    end_date = str(scorecard.get("end_date", "") or "").strip()
    total_matches = int(scorecard.get("total_matches", 0) or 0)
    lines = [f"DOGE scorecard {start_date} -> {end_date}"]
    if total_matches <= 0:
        lines.append("Sin cierres DOGE con contexto de estrategia en esta ventana.")
        return lines

    lines.append(
        f"Cierres {total_matches} | PnL {scorecard.get('realized_pnl_usd', '0')} USD | win rate {scorecard.get('win_rate_pct', '0')}%"
    )
    for strategy in list(scorecard.get("strategies") or [])[:3]:
        if not isinstance(strategy, dict):
            continue
        label = _strategy_label(str(strategy.get("strategy_id", "") or ""))
        top_regime = "n/d"
        regimes = list(strategy.get("regimes") or [])
        if regimes and isinstance(regimes[0], dict):
            top_regime = str(regimes[0].get("regime_label", "") or "").strip() or "n/d"
        lines.append(
            f"{label}: {strategy.get('closed_positions', 0)} cierres | PnL {strategy.get('realized_pnl_usd', '0')} USD | win rate {strategy.get('win_rate_pct', '0')}% | regime {top_regime}"
        )
    return lines