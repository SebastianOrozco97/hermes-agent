from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from tools.binance_live_adapter import BinanceLiveExecutionError
from tools.doge_premium_flow import (
    build_doge_adjustment_premium_payload,
    build_doge_entry_premium_payload,
)
from tools.doge_strategy_selector import StrategyOpportunity, select_doge_strategy


class _DummySignal:
    def __init__(self, **payload):
        self._payload = payload

    def to_dict(self) -> dict[str, object]:
        return dict(self._payload)


def _load_doge_live_scout_module():
    script_path = Path(__file__).resolve().parents[3] / "hermes_home" / "scripts" / "doge_live_scout.py"
    spec = importlib.util.spec_from_file_location("doge_live_scout_test_module", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_doge_strategy_router_module():
    script_path = Path(__file__).resolve().parents[3] / "hermes_home" / "scripts" / "doge_strategy_router.py"
    spec = importlib.util.spec_from_file_location("doge_strategy_router_test_module", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build_overlay_selection():
    overlay = StrategyOpportunity(
        strategy_id="overlay_tactical_long",
        symbol="DOGEUSDT",
        action="enter",
        eligible=True,
        blockers=(),
        expected_edge=Decimal("0.84"),
        confidence=Decimal("0.82"),
        capital_required_usd=Decimal("5.25"),
        holding_horizon="1h",
        macro_alignment="aligned",
        regime_tags=("overlay",),
        operator_summary="overlay tactico listo",
        diagnostic_payload={},
    )
    arbitrage = StrategyOpportunity(
        strategy_id="funding_arbitrage",
        symbol="DOGEUSDT",
        action="enter",
        eligible=True,
        blockers=(),
        expected_edge=Decimal("0.66"),
        confidence=Decimal("0.69"),
        capital_required_usd=Decimal("5.25"),
        holding_horizon="4h",
        macro_alignment="aligned",
        regime_tags=("arb",),
        operator_summary="arbitraje disponible",
        diagnostic_payload={},
    )
    return select_doge_strategy((overlay, arbitrage), conflict_margin=Decimal("0.05"))


def _configure_router_primary_scout_cycle(monkeypatch, tmp_path, module):
    class _Executor:
        def fetch_account_overview(self, symbol=None):
            return {
                "symbol": symbol,
                "account_snapshot": {
                    "free_balance_usd": "1000",
                    "open_positions": 0,
                    "positions_in_symbol": 0,
                    "daily_realized_pnl_usd": "0",
                    "kill_switch_active": False,
                },
            }

    class _Signal(SimpleNamespace):
        def to_dict(self):
            payload = {}
            for key, value in self.__dict__.items():
                payload[key] = format(value, "f") if isinstance(value, Decimal) else value
            return payload

    signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1010"),
        ema_fast=Decimal("0.1005"),
        ema_slow=Decimal("0.0998"),
        breakout_reference=Decimal("0.1008"),
        volume_ratio=Decimal("1.20"),
        signal_score=6,
        verifier_confidence=Decimal("0.81"),
        verdict="candidate_long",
        rationale="DOGE breakout structure remains intact.",
        market_summary="DOGE is pressing through the recent local range.",
    )
    context_signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="1h",
        last_close=Decimal("0.1012"),
        ema_fast=Decimal("0.1009"),
        ema_slow=Decimal("0.1001"),
        breakout_reference=Decimal("0.1005"),
        volume_ratio=Decimal("1.05"),
        signal_score=5,
        verifier_confidence=Decimal("0.75"),
        verdict="candidate_long",
        rationale="1h support remains constructive.",
        market_summary="1h structure stays favorable.",
    )
    selection = SimpleNamespace(
        chosen_strategy_id="overlay_tactical_long",
        abstained=False,
        abstain_reason="",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("DOGE_AUTONOMOUS_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("DOGE_AUTONOMOUS_GEMINI_ENABLED", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_POSITION_MANAGEMENT_ENABLED", "false")
    monkeypatch.setenv("DOGE_ROUTER_OWNS_ENTRY_APPROVAL", "true")
    monkeypatch.setattr(module.guarded, "_ensure_runtime_env_loaded", lambda: None)
    monkeypatch.setattr(module.guarded, "_doge_premium_analysis_enabled", lambda: False)
    monkeypatch.setattr(module.BinanceFuturesLiveExecutor, "from_env", staticmethod(lambda require_credentials=True: _Executor()))
    monkeypatch.setattr(module, "_analyze_timeframe", lambda executor, **kwargs: signal if kwargs.get("interval") == "15m" else context_signal)
    monkeypatch.setattr(module, "get_latest_trade_approval", lambda **kwargs: None)
    monkeypatch.setattr(module, "_has_recent_doge_approval", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        module.guarded,
        "_submit_trade_result",
        lambda **kwargs: {
            "success": True,
            "exchange_order_preview": {
                "reference_price": "0.10",
                "quantity": "52",
                "estimated_notional_usd": "5.25",
            },
        },
    )
    monkeypatch.setattr(module, "fetch_btc_macro_state", lambda: SimpleNamespace(to_dict=lambda: {"risk_level": "normal", "btc_trend_1h": "bullish", "btc_trend_4h": "bullish", "rationale": "ok"}))
    monkeypatch.setattr(module, "classify_macro_alignment", lambda snapshot: "aligned")
    monkeypatch.setattr(module, "build_live_strategy_selection", lambda *args, **kwargs: selection)
    monkeypatch.setattr(module, "request_trade_approval", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("router should own approval creation")))

    return selection


def _configure_entry_approval_scout_cycle(monkeypatch, tmp_path, module, *, premium_request=None):
    class _Executor:
        def fetch_account_overview(self, symbol=None):
            return {
                "symbol": symbol,
                "account_snapshot": {
                    "free_balance_usd": "1000",
                    "open_positions": 0,
                    "positions_in_symbol": 0,
                    "daily_realized_pnl_usd": "0",
                    "kill_switch_active": False,
                },
            }

    class _Signal(SimpleNamespace):
        def to_dict(self):
            payload = {}
            for key, value in self.__dict__.items():
                payload[key] = format(value, "f") if isinstance(value, Decimal) else value
            return payload

    signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1010"),
        ema_fast=Decimal("0.1005"),
        ema_slow=Decimal("0.0998"),
        breakout_reference=Decimal("0.1008"),
        volume_ratio=Decimal("1.20"),
        signal_score=6,
        verifier_confidence=Decimal("0.81"),
        verdict="candidate_long",
        rationale="DOGE breakout structure remains intact.",
        market_summary="DOGE is pressing through the recent local range.",
    )
    context_signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="1h",
        last_close=Decimal("0.1012"),
        ema_fast=Decimal("0.1009"),
        ema_slow=Decimal("0.1001"),
        breakout_reference=Decimal("0.1005"),
        volume_ratio=Decimal("1.05"),
        signal_score=5,
        verifier_confidence=Decimal("0.75"),
        verdict="candidate_long",
        rationale="1h support remains constructive.",
        market_summary="1h structure stays favorable.",
    )
    selection = SimpleNamespace(
        chosen_strategy_id="overlay_tactical_long",
        abstained=False,
        abstain_reason="",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("DOGE_AUTONOMOUS_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("DOGE_ROUTER_OWNS_ENTRY_APPROVAL", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_GEMINI_ENABLED", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_POSITION_MANAGEMENT_ENABLED", "false")
    monkeypatch.setattr(module.guarded, "_ensure_runtime_env_loaded", lambda: None)
    monkeypatch.setattr(module.guarded, "_doge_premium_analysis_enabled", lambda: True)
    monkeypatch.setattr(module.BinanceFuturesLiveExecutor, "from_env", staticmethod(lambda require_credentials=True: _Executor()))
    monkeypatch.setattr(module, "_analyze_timeframe", lambda executor, **kwargs: signal if kwargs.get("interval") == "15m" else context_signal)
    monkeypatch.setattr(module, "get_latest_trade_approval", lambda **kwargs: None)
    monkeypatch.setattr(module, "_has_recent_doge_approval", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        module.guarded,
        "_submit_trade_result",
        lambda **kwargs: {
            "success": True,
            "exchange_order_preview": {
                "reference_price": "0.10",
                "quantity": "52",
                "estimated_notional_usd": "5.25",
            },
        },
    )
    monkeypatch.setattr(module, "fetch_btc_macro_state", lambda: SimpleNamespace(to_dict=lambda: {"risk_level": "normal", "btc_trend_1h": "bullish", "btc_trend_4h": "bullish", "rationale": "ok"}))
    monkeypatch.setattr(module, "classify_macro_alignment", lambda snapshot: "aligned")
    monkeypatch.setattr(module, "record_market_evidence", lambda **kwargs: {"evidence_id": "EVID-1"})
    monkeypatch.setattr(module, "build_live_strategy_selection", lambda *args, **kwargs: selection)
    monkeypatch.setattr(
        module,
        "build_strategy_decision_context",
        lambda current_selection, **kwargs: {
            "selected_strategy": {"strategy_id": current_selection.chosen_strategy_id},
            "alternatives_considered": [{"strategy_id": "atr_grid"}],
            "macro_state": kwargs["macro_state"],
        },
    )
    monkeypatch.setattr(module, "material_fingerprint", lambda payload: "PREM-MATCH")
    monkeypatch.setattr(module, "request_doge_premium_analysis", lambda **kwargs: (_ for _ in ()).throw(AssertionError("should reuse existing premium request")))
    monkeypatch.setattr(
        module,
        "get_latest_doge_premium_analysis_request",
        lambda **kwargs: None if premium_request is None else {
            "request_id": "PREM-123",
            "symbol": "DOGEUSDT",
            "request_kind": "entry",
            "model": "gemini-3.5-flash",
            "event_fingerprint": "PREM-MATCH",
            **premium_request,
        },
    )

    return selection


def _configure_phase2_scout_cycle(monkeypatch, tmp_path, module, *, router_owns_entry_approval: bool):
    class _Executor:
        def fetch_account_overview(self, symbol=None):
            return {
                "symbol": symbol,
                "account_snapshot": {
                    "free_balance_usd": "1000",
                    "open_positions": 0,
                    "positions_in_symbol": 0,
                    "daily_realized_pnl_usd": "0",
                    "kill_switch_active": False,
                },
            }

    class _Signal(SimpleNamespace):
        def to_dict(self):
            payload = {}
            for key, value in self.__dict__.items():
                payload[key] = format(value, "f") if isinstance(value, Decimal) else value
            return payload

    signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1010"),
        ema_fast=Decimal("0.1005"),
        ema_slow=Decimal("0.0998"),
        breakout_reference=Decimal("0.1008"),
        volume_ratio=Decimal("0.95"),
        signal_score=3,
        verifier_confidence=Decimal("0.58"),
        verdict="standby",
        rationale="overlay sin breakout confirmado, pero DOGE sigue liquido.",
        market_summary="DOGE mantiene liquidez estable sin breakout direccional.",
    )
    context_signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="1h",
        last_close=Decimal("0.1012"),
        ema_fast=Decimal("0.1009"),
        ema_slow=Decimal("0.1001"),
        breakout_reference=Decimal("0.1005"),
        volume_ratio=Decimal("1.02"),
        signal_score=4,
        verifier_confidence=Decimal("0.66"),
        verdict="standby",
        rationale="1h estable, sin sesgo fuerte.",
        market_summary="1h neutro con sesgo lateral.",
    )
    selection = SimpleNamespace(
        chosen_strategy_id="funding_arbitrage",
        abstained=False,
        abstain_reason="",
        chosen_opportunity=SimpleNamespace(
            strategy_id="funding_arbitrage",
            operator_summary="carry positivo con funding favorable y delta neutral controlado",
            confidence=Decimal("0.74"),
            diagnostic_payload={
                "plan": {
                    "action": "enter_arbitrage",
                    "symbol": "DOGEUSDT",
                    "spot_quantity": "60",
                    "futures_quantity": "60",
                    "leverage": "2",
                    "spot_notional_usd": "6.18",
                    "futures_notional_usd": "6.18",
                    "futures_margin_usd": "3.09",
                    "delta_gap_pct": "0.02",
                    "expected_yield_pct": "0.18",
                    "rationale": "funding favorable y spread controlado",
                }
            },
        ),
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("DOGE_AUTONOMOUS_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("DOGE_AUTONOMOUS_GEMINI_ENABLED", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_POSITION_MANAGEMENT_ENABLED", "false")
    monkeypatch.setenv("DOGE_ROUTER_OWNS_ENTRY_APPROVAL", "true" if router_owns_entry_approval else "false")
    monkeypatch.setattr(module.guarded, "_ensure_runtime_env_loaded", lambda: None)
    monkeypatch.setattr(module.guarded, "_doge_premium_analysis_enabled", lambda: False)
    monkeypatch.setattr(module.BinanceFuturesLiveExecutor, "from_env", staticmethod(lambda require_credentials=True: _Executor()))
    monkeypatch.setattr(module, "_analyze_timeframe", lambda executor, **kwargs: signal if kwargs.get("interval") == "15m" else context_signal)
    monkeypatch.setattr(module, "get_latest_trade_approval", lambda **kwargs: None)
    monkeypatch.setattr(module, "_has_recent_doge_approval", lambda *args, **kwargs: False)
    monkeypatch.setattr(module, "fetch_btc_macro_state", lambda: SimpleNamespace(to_dict=lambda: {"risk_level": "normal", "btc_trend_1h": "neutral", "btc_trend_4h": "neutral", "rationale": "ok"}))
    monkeypatch.setattr(module, "classify_macro_alignment", lambda snapshot: "aligned")
    monkeypatch.setattr(module, "build_live_strategy_selection", lambda *args, **kwargs: selection)
    monkeypatch.setattr(
        module,
        "build_strategy_decision_context",
        lambda current_selection, **kwargs: {
            "selected_strategy": {"strategy_id": current_selection.chosen_strategy_id},
            "execution_request": kwargs.get("execution_request") or {},
            "macro_state": kwargs.get("macro_state") or {},
            "market_context": kwargs.get("market_context") or {},
        },
    )
    monkeypatch.setattr(module.guarded, "execute_arbitrage", lambda plan, dry_run=True: {"success": True, "execution_id": "ARB-PREVIEW", "dry_run": dry_run})
    monkeypatch.setattr(module, "record_market_evidence", lambda **kwargs: {"evidence_id": "EVID-1"})

    return selection


def test_doge_live_scout_degrades_binance_access_errors(monkeypatch, capsys, tmp_path):
    module = _load_doge_live_scout_module()

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("DOGE_AUTONOMOUS_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("DOGE_AUTONOMOUS_NOTIFY_BLOCKED", "true")
    monkeypatch.setattr(module.guarded, "_ensure_runtime_env_loaded", lambda: None)

    def _raise_access_error(*, require_credentials=True):
        raise BinanceLiveExecutionError(
            "Binance API GET /fapi/v2/account failed: Invalid API-key, IP, or permissions for action"
        )

    monkeypatch.setattr(module.BinanceFuturesLiveExecutor, "from_env", staticmethod(_raise_access_error))

    result = module.main()
    stdout = capsys.readouterr().out

    assert result == 0
    assert "live sin acceso operativo en Binance" in stdout
    assert "API key" in stdout
    assert "permisos Futures" in stdout


def test_doge_live_scout_requests_trade_approval_with_selector_context(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()

    class _Executor:
        def fetch_account_overview(self, symbol=None):
            return {
                "symbol": symbol,
                "account_snapshot": {
                    "free_balance_usd": "1000",
                    "open_positions": 0,
                    "positions_in_symbol": 0,
                    "daily_realized_pnl_usd": "0",
                    "kill_switch_active": False,
                },
            }

    class _Signal(SimpleNamespace):
        def to_dict(self):
            payload = {}
            for key, value in self.__dict__.items():
                payload[key] = format(value, "f") if isinstance(value, Decimal) else value
            return payload

    signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="15m",
        last_close=Decimal("0.1010"),
        ema_fast=Decimal("0.1005"),
        ema_slow=Decimal("0.0998"),
        breakout_reference=Decimal("0.1008"),
        volume_ratio=Decimal("1.20"),
        signal_score=6,
        verifier_confidence=Decimal("0.81"),
        verdict="candidate_long",
        rationale="DOGE breakout structure remains intact.",
        market_summary="DOGE is pressing through the recent local range.",
    )
    context_signal = _Signal(
        symbol="DOGEUSDT",
        timeframe="1h",
        last_close=Decimal("0.1012"),
        ema_fast=Decimal("0.1009"),
        ema_slow=Decimal("0.1001"),
        breakout_reference=Decimal("0.1005"),
        volume_ratio=Decimal("1.05"),
        signal_score=5,
        verifier_confidence=Decimal("0.75"),
        verdict="candidate_long",
        rationale="1h support remains constructive.",
        market_summary="1h structure stays favorable.",
    )
    selection = SimpleNamespace(
        chosen_strategy_id="overlay_tactical_long",
        abstained=False,
        abstain_reason="",
    )
    captured: dict[str, object] = {}

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("DOGE_AUTONOMOUS_ANALYSIS_ENABLED", "true")
    monkeypatch.setenv("BINANCE_RISK_MODE", "live")
    monkeypatch.setenv("BINANCE_LIVE_TRADING_ENABLED", "true")
    monkeypatch.setenv("DOGE_ROUTER_OWNS_ENTRY_APPROVAL", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_GEMINI_ENABLED", "false")
    monkeypatch.setenv("DOGE_AUTONOMOUS_POSITION_MANAGEMENT_ENABLED", "false")
    monkeypatch.setattr(module.guarded, "_ensure_runtime_env_loaded", lambda: None)
    monkeypatch.setattr(module.guarded, "_doge_premium_analysis_enabled", lambda: False)
    monkeypatch.setattr(module.BinanceFuturesLiveExecutor, "from_env", staticmethod(lambda require_credentials=True: _Executor()))
    monkeypatch.setattr(module, "_analyze_timeframe", lambda executor, **kwargs: signal if kwargs.get("interval") == "15m" else context_signal)
    monkeypatch.setattr(module, "get_latest_trade_approval", lambda **kwargs: None)
    monkeypatch.setattr(module, "_has_recent_doge_approval", lambda *args, **kwargs: False)
    monkeypatch.setattr(
        module.guarded,
        "_submit_trade_result",
        lambda **kwargs: {
            "success": True,
            "exchange_order_preview": {
                "reference_price": "0.10",
                "quantity": "52",
                "estimated_notional_usd": "5.25",
            },
        },
    )
    monkeypatch.setattr(module, "fetch_btc_macro_state", lambda: SimpleNamespace(to_dict=lambda: {"risk_level": "normal", "btc_trend_1h": "bullish", "btc_trend_4h": "bullish", "rationale": "ok"}))
    monkeypatch.setattr(module, "classify_macro_alignment", lambda snapshot: "aligned")
    monkeypatch.setattr(module, "record_market_evidence", lambda **kwargs: {"evidence_id": "EVID-1"})
    monkeypatch.setattr(module, "build_live_strategy_selection", lambda *args, **kwargs: selection)
    monkeypatch.setattr(
        module,
        "build_strategy_decision_context",
        lambda current_selection, **kwargs: {
            "selected_strategy": {"strategy_id": current_selection.chosen_strategy_id},
            "alternatives_considered": [{"strategy_id": "atr_grid"}],
            "macro_state": kwargs["macro_state"],
        },
    )
    monkeypatch.setattr(
        module,
        "request_trade_approval",
        lambda proposal, **kwargs: captured.update(kwargs) or {"approval_id": "TRADE-1", "expires_at": "2026-05-20T07:00:00+00:00"},
    )

    result = module.main()

    assert result == 0
    assert captured["requested_via"] == "cron_15m_doge"
    assert captured["decision_context"]["selected_strategy"]["strategy_id"] == "overlay_tactical_long"
    assert captured["decision_context"]["alternatives_considered"][0]["strategy_id"] == "atr_grid"


def test_doge_live_scout_surfaces_passed_entry_premium_status_before_approval(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    _configure_entry_approval_scout_cycle(
        monkeypatch,
        tmp_path,
        module,
        premium_request={
            "status": "completed",
            "analysis_outcome": "passed",
            "analysis": {
                "summary": "Gemini 3.5 Flash confirma la entrada.",
                "confidence": "0.86",
            },
        },
    )
    monkeypatch.setattr(
        module,
        "request_trade_approval",
        lambda proposal, **kwargs: {"approval_id": "TRADE-1", "expires_at": "2026-05-20T07:00:00+00:00"},
    )

    result = module.main(emit_output=False)

    assert result["status"] == "approval_created"
    assert "Estado premium: Gemini 3.5 Flash confirma DOGEUSDT | Conf 86.00%." in result["lines"]
    assert "Premium: Gemini 3.5 Flash confirma la entrada." in result["lines"]


def test_doge_live_scout_surfaces_denied_entry_premium_fallback_status_before_approval(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    _configure_entry_approval_scout_cycle(
        monkeypatch,
        tmp_path,
        module,
        premium_request={
            "status": "denied",
            "analysis_outcome": None,
            "analysis": {},
        },
    )
    monkeypatch.setattr(
        module,
        "request_trade_approval",
        lambda proposal, **kwargs: {"approval_id": "TRADE-1", "expires_at": "2026-05-20T07:00:00+00:00"},
    )

    result = module.main(emit_output=False)

    assert result["status"] == "approval_created"
    assert "Estado premium: omitido por operador | fallback al flujo actual." in result["lines"]


def test_doge_live_scout_defers_entry_approval_to_router_when_configured(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    selection = _configure_router_primary_scout_cycle(monkeypatch, tmp_path, module)

    result = module.main(emit_output=False)

    assert result["status"] == "router_primary"
    assert result["selection"].chosen_strategy_id == "overlay_tactical_long"
    assert any("router DOGE" in line for line in result["lines"])


def test_doge_live_scout_defers_phase2_router_primary_without_overlay_gate(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    selection = _configure_phase2_scout_cycle(monkeypatch, tmp_path, module, router_owns_entry_approval=True)

    result = module.main(emit_output=False)

    assert result["status"] == "router_primary"
    assert result["selection"].chosen_strategy_id == "funding_arbitrage"
    assert any("Arbitraje de funding" in line for line in result["lines"])


def test_doge_live_scout_creates_phase2_approval_with_execution_request(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    _configure_phase2_scout_cycle(monkeypatch, tmp_path, module, router_owns_entry_approval=False)
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "request_trade_approval",
        lambda proposal, **kwargs: captured.update({"proposal": proposal.to_dict(), **kwargs}) or {
            "approval_id": "TRADE-ARB1",
            "expires_at": "2026-05-20T07:00:00+00:00",
            "symbol": "DOGEUSDT",
            "status": "pending",
            "decision_context": kwargs.get("decision_context") or {},
        },
    )

    result = module.main(emit_output=False)

    assert result["status"] == "approval_created"
    assert captured["decision_context"]["execution_request"]["kind"] == "funding_arbitrage"
    assert captured["decision_context"]["execution_request"]["plan"]["expected_yield_pct"] == "0.18"
    assert captured["proposal"]["notional_usd"] == "9.27"
    assert any("Fase 2 Arbitraje de funding" in line for line in result["lines"])


def test_doge_live_scout_creates_phase3_approval_with_execution_request(monkeypatch, tmp_path):
    module = _load_doge_live_scout_module()
    selection = _configure_phase2_scout_cycle(monkeypatch, tmp_path, module, router_owns_entry_approval=False)
    selection.chosen_strategy_id = "atr_grid"
    selection.chosen_opportunity.strategy_id = "atr_grid"
    selection.chosen_opportunity.operator_summary = "rango tranquilo con ATR contenido y sesgo lateral"
    selection.chosen_opportunity.diagnostic_payload = {
        "plan": {
            "symbol": "DOGEUSDT",
            "market_price": "0.1010",
            "levels": [
                {"price": "0.0990", "side": "BUY", "quantity": "20"},
                {"price": "0.1030", "side": "SELL", "quantity": "20"},
            ],
            "total_required_capital": "12",
            "stop_loss_price_lower": "0.0970",
            "stop_loss_price_upper": "0.1050",
            "leverage": "1",
            "regime": "range_bound",
            "regime_reason": "volatilidad comprimida",
            "regime_allows_entry": True,
            "rationale": "desplegar grid alrededor del precio medio",
        }
    }
    monkeypatch.setattr(module.guarded, "execute_arbitrage", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not use arbitrage preview")))
    monkeypatch.setattr(module.guarded, "execute_grid", lambda plan, dry_run=True: {"success": True, "execution_id": "GRID-PREVIEW", "dry_run": dry_run})
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        module,
        "request_trade_approval",
        lambda proposal, **kwargs: captured.update({"proposal": proposal.to_dict(), **kwargs}) or {
            "approval_id": "TRADE-GRID1",
            "expires_at": "2026-05-20T07:00:00+00:00",
            "symbol": "DOGEUSDT",
            "status": "pending",
            "decision_context": kwargs.get("decision_context") or {},
        },
    )

    result = module.main(emit_output=False)

    assert result["status"] == "approval_created"
    assert captured["decision_context"]["execution_request"]["kind"] == "atr_grid"
    assert captured["decision_context"]["execution_request"]["plan"]["regime"] == "range_bound"
    assert captured["proposal"]["notional_usd"] == "12"
    assert any("Fase 3 ATR grid" in line for line in result["lines"])


def test_doge_live_scout_stays_quiet_when_router_owns_entry_flow(monkeypatch, tmp_path, capsys):
    module = _load_doge_live_scout_module()
    _configure_router_primary_scout_cycle(monkeypatch, tmp_path, module)

    result = module.main()
    stdout = capsys.readouterr().out

    assert result == 0
    assert stdout == ""


def test_doge_live_scout_can_reenable_diagnostics_explicitly(monkeypatch, tmp_path, capsys):
    module = _load_doge_live_scout_module()
    monkeypatch.setenv("DOGE_AUTONOMOUS_SCOUT_DIAGNOSTIC_OUTPUT", "true")
    _configure_router_primary_scout_cycle(monkeypatch, tmp_path, module)

    result = module.main()
    stdout = capsys.readouterr().out

    assert result == 0
    assert "router primario origina la aprobacion" in stdout.lower()


def test_doge_strategy_router_delegates_entry_creation_to_scout_cycle(monkeypatch):
    module = _load_doge_strategy_router_module()
    captured: dict[str, object] = {}
    selection = _build_overlay_selection()

    def _fake_cycle(**kwargs):
        captured.update(kwargs)
        return {
            "status": "approval_created",
            "selection": selection,
            "lines": ["APPROVAL LINE"],
        }

    monkeypatch.setattr(module, "run_doge_live_scout_cycle", _fake_cycle)

    def _capture_emit(lines):
        captured["emitted"] = list(lines)
        return 0

    monkeypatch.setattr(module, "_emit", _capture_emit)

    result = module.main()

    assert result == 0
    assert captured["emit_output"] is False
    assert captured["create_entry_actions"] is True
    assert captured["management_actions_enabled"] is False
    assert captured["requested_via"] == "cron_15m_doge_router"
    emitted = captured["emitted"]
    assert emitted[0] == "DOGE STRATEGY ROUTER (DOGEUSDT)"
    assert "APPROVAL LINE" in emitted


def test_entry_premium_payload_serializes_single_macro_state_key():
    payload = build_doge_entry_premium_payload(
        symbol="DOGEUSDT",
        timeframe="15m",
        signal=_DummySignal(verdict="candidate_long", signal_score=6),
        contextual_signals={"1h": _DummySignal(verdict="candidate_long", signal_score=5)},
        exchange_preview={"reference_price": "0.10"},
        notional_usd=Decimal("5.25"),
        stop_loss_pct=Decimal("0.5"),
        take_profit_pct=Decimal("1.0"),
        leverage=Decimal("1"),
        base_rationale="setup tactico",
        market_summary="DOGE en rango alto",
        gemini_lite_assessment={"summary": "ok"},
        proposal_payload={"symbol": "DOGEUSDT"},
        evidence_id="EVID-1",
        macro_state={"risk_level": "elevated"},
    )

    encoded = json.dumps(payload, sort_keys=True)

    assert encoded.count('"macro_state"') == 1
    assert payload["macro_state"]["risk_level"] == "elevated"


def test_adjustment_premium_payload_serializes_single_macro_state_key():
    snapshot = SimpleNamespace(
        symbol="DOGEUSDT",
        timeframe="15m",
        approval_id="TRADE-1",
        entry_side="BUY",
        signal=_DummySignal(verdict="manage", signal_score=4, last_close="0.10"),
        contextual_signals={"1h": _DummySignal(verdict="support", signal_score=4)},
        active_position={"side": "LONG", "entry_price": "0.095"},
        protective_orders={
            "stop_loss": {"orderId": 1},
            "take_profit": {"orderId": 2},
            "stop_loss_price": "0.090",
            "take_profit_price": "0.110",
        },
        recommended_stop_price=Decimal("0.091"),
        recommended_take_profit_price=Decimal("0.108"),
        protective_orders_missing=False,
        plan=SimpleNamespace(
            action="tighten_protection",
            summary="subir stop",
            rationale="mejora de estructura",
            unrealized_pnl_usd=Decimal("1.2"),
            pnl_pct=Decimal("3.4"),
            higher_timeframe_support=2,
            higher_timeframe_total=2,
        ),
    )

    payload = build_doge_adjustment_premium_payload(
        snapshot,
        timeframe="15m",
        macro_state={"risk_level": "contained"},
    )
    encoded = json.dumps(payload, sort_keys=True)

    assert encoded.count('"macro_state"') == 1
    assert payload["adjustment_context"]["macro_state"]["risk_level"] == "contained"