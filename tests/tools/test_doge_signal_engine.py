from __future__ import annotations

from tools.doge_signal_engine import analyze_doge_15m_signal, parse_binance_klines


def _build_klines(closes: list[str], *, last_volume: str = "1800") -> list[list[str]]:
    rows: list[list[str]] = []
    open_time = 1_700_000_000_000
    previous_close = closes[0]
    for index, close in enumerate(closes):
        open_price = previous_close if index else close
        high_price = str(max(float(open_price), float(close)) + 0.0004)
        low_price = str(min(float(open_price), float(close)) - 0.0004)
        volume = last_volume if index == len(closes) - 1 else "1000"
        rows.append(
            [
                str(open_time),
                str(open_price),
                high_price,
                low_price,
                str(close),
                volume,
                str(open_time + 899_999),
            ]
        )
        open_time += 900_000
        previous_close = close
    return rows


def test_parse_binance_klines_requires_minimum_rows():
    closes = [f"0.100{i}" for i in range(30)]
    candles = parse_binance_klines(_build_klines(closes))

    assert len(candles) == 30
    assert candles[0].close == candles[0].open


def test_analyze_doge_15m_signal_identifies_candidate_long():
    closes = [
        "0.1000", "0.1006", "0.1003", "0.1010", "0.1008", "0.1014", "0.1011", "0.1018",
        "0.1015", "0.1021", "0.1019", "0.1026", "0.1023", "0.1029", "0.1026", "0.1032",
        "0.1030", "0.1037", "0.1034", "0.1040", "0.1037", "0.1044", "0.1041", "0.1048",
        "0.1045", "0.1051", "0.1049", "0.1056", "0.1053", "0.1059", "0.1056", "0.1062",
        "0.1059", "0.1065", "0.1062", "0.1069", "0.1066", "0.1072", "0.1069", "0.1076",
    ]
    snapshot = analyze_doge_15m_signal(parse_binance_klines(_build_klines(closes)))

    assert snapshot.verdict == "candidate_long"
    assert snapshot.signal_score >= 5
    assert snapshot.verifier_confidence >= snapshot.verifier_confidence.__class__("0.75")


def test_analyze_doge_15m_signal_stays_on_standby_without_trend_confirmation():
    closes = [
        "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001",
        "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001",
        "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001",
        "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001",
        "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001", "0.1000", "0.1001",
    ]
    snapshot = analyze_doge_15m_signal(parse_binance_klines(_build_klines(closes, last_volume="900")))

    assert snapshot.verdict == "standby"
    assert snapshot.signal_score < 5