"""E2E tests for gateway slash commands (Telegram, Discord).

Each test drives a message through the full async pipeline:
    adapter.handle_message(event)
        → BasePlatformAdapter._process_message_background()
        → GatewayRunner._handle_message() (command dispatch)
        → adapter.send() (captured for assertions)

No LLM involved — only gateway-level commands are tested.
Tests are parametrized over platforms via the ``platform`` fixture in conftest.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from tests.e2e.conftest import make_event, send_and_capture


class TestSlashCommands:
    """Gateway slash commands dispatched through the full adapter pipeline."""

    @pytest.mark.asyncio
    async def test_help_returns_command_list(self, adapter, platform):
        send = await send_and_capture(adapter, "/help", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "/new" in response_text
        assert "/status" in response_text

    @pytest.mark.asyncio
    async def test_status_shows_session_info(self, adapter, platform):
        send = await send_and_capture(adapter, "/status", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "session" in response_text.lower() or "Session" in response_text

    @pytest.mark.asyncio
    async def test_new_resets_session(self, adapter, runner, platform):
        send = await send_and_capture(adapter, "/new", platform)

        send.assert_called_once()
        runner.session_store.reset_session.assert_called_once()

    @pytest.mark.asyncio
    async def test_stop_when_no_agent_running(self, adapter, platform):
        send = await send_and_capture(adapter, "/stop", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        response_lower = response_text.lower()
        assert "no" in response_lower or "stop" in response_lower or "not running" in response_lower

    @pytest.mark.asyncio
    async def test_commands_shows_listing(self, adapter, platform):
        send = await send_and_capture(adapter, "/commands", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Should list at least some commands
        assert "/" in response_text

    @pytest.mark.asyncio
    async def test_sequential_commands_share_session(self, adapter, platform):
        """Two commands from the same chat_id should both succeed."""
        send_help = await send_and_capture(adapter, "/help", platform)
        send_help.assert_called_once()

        send_status = await send_and_capture(adapter, "/status", platform)
        send_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_verbose_responds(self, adapter, platform):
        send = await send_and_capture(adapter, "/verbose", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Either shows the mode cycle or tells user to enable it in config
        assert "verbose" in response_text.lower() or "tool_progress" in response_text

    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_routes_to_safe_restart_command(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Plaintext restart shortcut is intentionally DM/Telegram-focused")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)

        send = await send_and_capture(adapter, "restart gateway", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "restart" in response_text.lower() or "draining" in response_text.lower()
        runner.request_restart.assert_called_once_with(detached=False, via_service=True)

    @pytest.mark.asyncio
    async def test_plaintext_restart_gateway_in_group_stays_plain_text(self, adapter, runner, platform, monkeypatch):
        if platform != Platform.TELEGRAM:
            pytest.skip("Shortcut scope is only verified for Telegram here")

        monkeypatch.setenv("INVOCATION_ID", "e2e-systemd")
        runner.request_restart = MagicMock(return_value=True)
        runner._handle_message_with_agent = AsyncMock(return_value="agent-handled")

        send = await send_and_capture(adapter, "restart gateway", platform, chat_id="group-chat-1", user_id="u1", chat_type="group")

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "agent-handled"
        runner.request_restart.assert_not_called()

    @pytest.mark.asyncio
    async def test_plaintext_trade_status_id_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_paper_position_status_result",
            lambda **kwargs: {
                "success": True,
                "status": "closed",
                "position": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "position_id": "PPOS-123",
                    "approval_id": "TRADE-123",
                },
                "closed_at": "2026-05-19T01:06:05+00:00",
                "exit_price": "0.10499",
                "trigger": "manual",
                "realized_pnl_usd": "0.13",
                "realized_pnl_pct": "0.66",
                "duration_human": "3h 46m 56s",
                "reason": "Solicitado por el usuario por chat.",
                "commands": {"status_trade": "ESTADO TRADE-123"},
            },
        )

        send = await send_and_capture(adapter, "ESTADO TRADE-123", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Paper cerrado DOGEUSDT BUY | PPOS-123" in response_text
        assert "Seguimiento: ESTADO TRADE-123" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_status_in_group_stays_plain_text(self, adapter, runner, platform):
        runner._handle_message_with_agent = AsyncMock(return_value="agent-handled")

        send = await send_and_capture(
            adapter,
            "ESTADO TRADE-123",
            platform,
            chat_id="group-chat-1",
            user_id="u1",
            chat_type="group",
        )

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "agent-handled"
        runner._handle_message_with_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "record_trade_approval",
            lambda approval_id, **kwargs: {
                "approval_id": approval_id,
                "status": "approved",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "2",
                    "mode": "live",
                    "order_type": "MARKET",
                    "stop_loss_pct": "0.5",
                    "take_profit_pct": "1",
                    "leverage": "1",
                    "verifier_model": "doge-scout-v1",
                    "verifier_passed": True,
                    "verifier_confidence": "0.88",
                },
            },
        )
        monkeypatch.setattr(
            guarded,
            "_submit_trade_result",
            lambda **kwargs: {
                "success": True,
                "execution_mode": "live",
                "approval": {"approval_id": "TRADE-123"},
                "decision": {
                    "proposal": {
                        "symbol": "DOGEUSDT",
                        "side": "BUY",
                        "notional_usd": "2",
                        "leverage": "1",
                    }
                },
                "execution": {
                    "quantity": "20",
                    "entry_order": {
                        "avgPrice": "0.1001",
                        "executedQty": "20",
                        "status": "FILLED",
                        "updateTime": 1779268200000,
                    },
                    "protective_orders": {
                        "stop_loss_price": "0.0996",
                        "take_profit_price": "0.1011",
                    },
                },
            },
        )

        send = await send_and_capture(adapter, "APROBAR TRADE-123", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Live ejecutado BUY DOGEUSDT | TRADE-123" in response_text
        assert "esperar radar 15m" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_status_symbol_returns_latest_approval(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_latest_trade_approval",
            lambda **kwargs: {
                "approval_id": "TRADE-123",
                "status": "pending",
                "symbol": "DOGEUSDT",
                "created_at": "2026-05-20T06:30:04+00:00",
                "expires_at": "2026-05-20T07:00:04+00:00",
                "market_summary": "DOGE mantiene sesgo alcista en 15m con medias ascendentes.",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "5.25",
                    "stop_loss_pct": "0.5",
                    "take_profit_pct": "1.0",
                },
            },
        )

        send = await send_and_capture(adapter, "ESTADO DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Aprobacion TRADE-123 pendiente" in response_text
        assert "APROBAR DOGE" in response_text
        assert "ESTADO DOGE" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_status_symbol_prefers_pending_premium_request(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_latest_doge_premium_analysis_request",
            lambda **kwargs: {
                "request_id": "PREM-123",
                "status": "pending",
                "symbol": "DOGEUSDT",
                "request_kind": "adjustment",
                "model": "gemini-3.5-flash",
                "expires_at": "2026-05-20T07:00:00+00:00",
                "material_payload": {
                    "adjustment_context": {
                        "summary": "subir SL para asegurar beneficio",
                    }
                },
            },
        )
        monkeypatch.setattr(paper_runtime, "get_latest_trade_approval", lambda **kwargs: None)

        send = await send_and_capture(adapter, "ESTADO DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Analisis premium pendiente DOGEUSDT" in response_text
        assert "ANALIZAR DOGE" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_symbol_resolves_latest_pending(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_latest_trade_approval",
            lambda **kwargs: {
                "approval_id": "TRADE-123",
                "status": "pending",
                "symbol": "DOGEUSDT",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "5.25",
                    "mode": "live",
                    "order_type": "MARKET",
                    "stop_loss_pct": "0.5",
                    "take_profit_pct": "1",
                    "leverage": "1",
                    "verifier_model": "doge-scout-v1",
                    "verifier_passed": True,
                    "verifier_confidence": "0.88",
                },
            },
        )
        record_approval = MagicMock(
            return_value={
                "approval_id": "TRADE-123",
                "status": "approved",
                "evidence_id": "EVID-123",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "5.25",
                    "mode": "live",
                    "order_type": "MARKET",
                    "stop_loss_pct": "0.5",
                    "take_profit_pct": "1",
                    "leverage": "1",
                    "verifier_model": "doge-scout-v1",
                    "verifier_passed": True,
                    "verifier_confidence": "0.88",
                },
            }
        )
        monkeypatch.setattr(guarded, "record_trade_approval", record_approval)
        monkeypatch.setattr(
            guarded,
            "_submit_trade_result",
            lambda **kwargs: {
                "success": True,
                "execution_mode": "live",
                "approval": {"approval_id": "TRADE-123"},
                "decision": {
                    "proposal": {
                        "symbol": "DOGEUSDT",
                        "side": "BUY",
                        "notional_usd": "5.25",
                        "leverage": "1",
                    }
                },
                "execution": {
                    "quantity": "50",
                    "entry_order": {
                        "avgPrice": "0.1036",
                        "executedQty": "50",
                        "status": "FILLED",
                        "updateTime": 1779268200000,
                    },
                    "protective_orders": {
                        "stop_loss_price": "0.1031",
                        "take_profit_price": "0.1046",
                    },
                },
            },
        )

        send = await send_and_capture(adapter, "APROBAR DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Live ejecutado BUY DOGEUSDT | TRADE-123" in response_text
        assert "esperar radar 15m" in response_text
        record_approval.assert_called_once()
        assert record_approval.call_args.args[0] == "TRADE-123"
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_symbol_executes_strategy_request(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_latest_trade_approval",
            lambda **kwargs: {
                "approval_id": "TRADE-123",
                "status": "pending",
                "symbol": "DOGEUSDT",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "9.27",
                    "mode": "live",
                    "order_type": "MARKET",
                },
            },
        )
        record_approval = MagicMock(
            return_value={
                "approval_id": "TRADE-123",
                "status": "approved",
                "symbol": "DOGEUSDT",
                "decision_context": {
                    "selected_strategy_id": "funding_arbitrage",
                    "selected_strategy": {"strategy_id": "funding_arbitrage"},
                    "execution_request": {
                        "kind": "funding_arbitrage",
                        "strategy_id": "funding_arbitrage",
                        "summary": "carry positivo con funding favorable",
                        "plan": {
                            "spot_notional_usd": "6.18",
                            "futures_notional_usd": "6.18",
                            "futures_margin_usd": "3.09",
                            "leverage": "2",
                            "expected_yield_pct": "0.18",
                            "delta_gap_pct": "0.02",
                        },
                    },
                },
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "9.27",
                    "mode": "live",
                    "order_type": "MARKET",
                },
            }
        )
        submit_trade = MagicMock()
        monkeypatch.setattr(guarded, "record_trade_approval", record_approval)
        monkeypatch.setattr(guarded, "_submit_trade_result", submit_trade)
        monkeypatch.setattr(
            guarded,
            "_execute_strategy_approval",
            lambda approval, notify_whatsapp=False: {
                "success": True,
                "execution_mode": "live-strategy",
                "strategy_id": "funding_arbitrage",
                "symbol": "DOGEUSDT",
                "approval": {"approval_id": "TRADE-123", "symbol": "DOGEUSDT"},
                "execution_request": {
                    "plan": {
                        "spot_notional_usd": "6.18",
                        "futures_notional_usd": "6.18",
                        "futures_margin_usd": "3.09",
                        "leverage": "2",
                        "expected_yield_pct": "0.18",
                        "delta_gap_pct": "0.02",
                    }
                },
                "execution": {"execution_id": "ARB-123"},
            },
        )

        send = await send_and_capture(adapter, "APROBAR DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Live arbitraje ejecutado DOGEUSDT | TRADE-123" in response_text
        assert "Yield esp 0.18%" in response_text
        submit_trade.assert_not_called()
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_in_group_stays_plain_text(self, adapter, runner, platform):
        runner._handle_message_with_agent = AsyncMock(return_value="agent-handled")

        send = await send_and_capture(
            adapter,
            "APROBAR TRADE-123",
            platform,
            chat_id="group-chat-1",
            user_id="u1",
            chat_type="group",
        )

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "agent-handled"
        runner._handle_message_with_agent.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_plaintext_trade_adjustment_symbol_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_adjust_live_trade_protection_result",
            lambda **kwargs: {
                "success": True,
                "symbol": "DOGEUSDT",
                "management": {
                    "symbol": "DOGEUSDT",
                    "approval_id": "TRADE-123",
                    "market_price": "0.10080",
                    "unrealized_pnl_usd": "0.40",
                    "unrealized_pnl_pct": "0.80",
                    "current_stop_price": "0.09950",
                    "current_take_profit_price": "0.10100",
                    "recommended_stop_price": "0.10020",
                    "recommended_take_profit_price": "0.10120",
                    "summary": "subir SL para asegurar buena parte del beneficio",
                },
            },
        )

        send = await send_and_capture(adapter, "AJUSTAR DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Ajuste live DOGEUSDT | TRADE-123" in response_text
        assert "esperar radar 15m" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_phase1_status_symbol_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_doge_phase1_status_result",
            lambda **kwargs: {
                "success": True,
                "symbol": "DOGEUSDT",
                "lines": [
                    "FASE 1: OVERLAY TACTICO (DOGEUSDT)",
                    "Estado: en espera | macro aligned | regimen quiet_range | horizonte 1h | edge 0.28 | confianza 0.6",
                    "Fase 1 detalle: 15m 2/7 standby @0.09973 | breakout 0.10036 | EMA9 0.09956 | EMA21 0.09943 | RSI 42.5 | vol 0.6x",
                    "Fase 1 gatillo: recuperar breakout 0.10036 con volumen y sostener EMA21 0.09943.",
                    "Prioridad router: ATR grid -> enter.",
                    "Fase 1 bloqueo actual: signal verdict is standby.",
                ],
                "pending_premium_request": None,
                "pending_approval": None,
            },
        )

        send = await send_and_capture(adapter, "VER FASE 1 DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "FASE 1: OVERLAY TACTICO (DOGEUSDT)" in response_text
        assert "Fase 1 bloqueo actual: signal verdict is standby." in response_text
        assert "ESTADO FASE 1 DOGE" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_phase1_status_alias_estado_symbol_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_doge_phase1_status_result",
            lambda **kwargs: {
                "success": True,
                "symbol": "DOGEUSDT",
                "lines": ["FASE 1: OVERLAY TACTICO (DOGEUSDT)"],
                "pending_premium_request": None,
                "pending_approval": None,
            },
        )

        send = await send_and_capture(adapter, "ESTADO FASE 1 DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "FASE 1: OVERLAY TACTICO (DOGEUSDT)" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_phase1_simulation_freeform_live_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        simulation_result = {
            "success": True,
            "symbol": "DOGEUSDT",
            "requested_mode": "live",
            "lines": [
                "FASE 1: OVERLAY TACTICO (DOGEUSDT)",
                "Prioridad router: Fase 1 es la estrategia primaria en este ciclo.",
            ],
            "trade_profile": {
                "notional_usd": "5.25",
                "stop_loss_pct": "0.5",
                "take_profit_pct": "1.0",
                "leverage": "1",
            },
            "preview": {
                "success": True,
                "decision": {
                    "proposal": {"mode": "live"},
                    "reasons": [],
                },
                "exchange_order_preview": {
                    "reference_price": "0.10010",
                    "quantity": "52",
                    "estimated_notional_usd": "5.21",
                },
            },
            "pending_premium_request": None,
            "pending_approval": None,
        }
        simulation_mock = MagicMock(return_value=simulation_result)
        monkeypatch.setattr(guarded, "_doge_phase1_simulation_result", simulation_mock)

        send = await send_and_capture(adapter, "Simula fase 1 para modo live", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "SIMULACION FASE 1 (DOGEUSDT) | modo solicitado live" in response_text
        assert "Preview exchange: ref" in response_text
        assert "qty 52" in response_text
        assert "notional 5.21 USD" in response_text
        simulation_mock.assert_called_once_with(symbol="DOGEUSDT", requested_mode="live")
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_phase1_analysis_question_routes_to_deterministic_simulation(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        simulation_result = {
            "success": True,
            "symbol": "DOGEUSDT",
            "requested_mode": "live",
            "lines": [
                "FASE 1: OVERLAY TACTICO (DOGEUSDT)",
                "Fase 1 bloqueo actual: signal verdict is standby.",
            ],
            "trade_profile": {
                "notional_usd": "5.25",
                "stop_loss_pct": "0.5",
                "take_profit_pct": "1.0",
                "leverage": "1",
            },
            "preview": None,
            "simulation_note": "No ejecuto la simulacion porque Fase 1 todavia no es la candidata primaria y aprobable del router.",
            "pending_premium_request": None,
            "pending_approval": None,
        }
        simulation_mock = MagicMock(return_value=simulation_result)
        monkeypatch.setattr(guarded, "_doge_phase1_simulation_result", simulation_mock)

        send = await send_and_capture(
            adapter,
            "Analisa fase 1 para ejecucion en modo live, esta bueno para invertir en este momento?",
            platform,
        )

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "SIMULACION FASE 1 (DOGEUSDT) | modo solicitado live" in response_text
        assert "Fase 1 bloqueo actual: signal verdict is standby." in response_text
        assert "APROBAR EVID-" not in response_text
        simulation_mock.assert_called_once_with(symbol="DOGEUSDT", requested_mode="live")
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_phase1_entry_intent_routes_to_deterministic_simulation(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        simulation_result = {
            "success": True,
            "symbol": "DOGEUSDT",
            "requested_mode": "live",
            "lines": [
                "FASE 1: OVERLAY TACTICO (DOGEUSDT)",
                "Fase 1 bloqueo actual: signal verdict is standby.",
            ],
            "trade_profile": {
                "notional_usd": "5.25",
                "stop_loss_pct": "0.5",
                "take_profit_pct": "1.0",
                "leverage": "1",
            },
            "preview": None,
            "simulation_note": "No ejecuto la simulacion porque Fase 1 todavia no es la candidata primaria y aprobable del router.",
            "pending_premium_request": None,
            "pending_approval": None,
        }
        simulation_mock = MagicMock(return_value=simulation_result)
        monkeypatch.setattr(guarded, "_doge_phase1_simulation_result", simulation_mock)

        send = await send_and_capture(
            adapter,
            "Entremos en corto para la fase 1 con los parametros que propones",
            platform,
        )

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "SIMULACION FASE 1 (DOGEUSDT) | modo solicitado live" in response_text
        assert "Fase 1 bloqueo actual: signal verdict is standby." in response_text
        assert "APROBAR EVID-" not in response_text
        simulation_mock.assert_called_once_with(symbol="DOGEUSDT", requested_mode="live")
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_evidence_id_returns_guidance(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_market_evidence",
            lambda evidence_id: {
                "evidence_id": evidence_id,
                "symbol": "DOGEUSDT",
            },
        )
        monkeypatch.setattr(
            paper_runtime,
            "get_latest_trade_approval",
            lambda **kwargs: {
                "approval_id": "TRADE-321",
                "status": "pending",
                "symbol": "DOGEUSDT",
                "evidence_id": "EVID-321",
            },
        )

        send = await send_and_capture(adapter, "APROBAR EVID-321", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "EVID-321 es evidencia de mercado, no una aprobacion." in response_text
        assert "APROBAR TRADE-321" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_approval_evidence_id_prefers_exact_linked_approval(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            paper_runtime,
            "get_market_evidence",
            lambda evidence_id: {
                "evidence_id": evidence_id,
                "symbol": "DOGEUSDT",
            },
        )

        def _fake_get_latest_trade_approval(**kwargs):
            if kwargs.get("status") == "pending" and kwargs.get("evidence_id") == "EVID-321":
                return {
                    "approval_id": "TRADE-321",
                    "status": "pending",
                    "symbol": "DOGEUSDT",
                    "evidence_id": "EVID-321",
                }
            if kwargs.get("status") == "pending" and kwargs.get("symbol") == "DOGEUSDT":
                return {
                    "approval_id": "TRADE-999",
                    "status": "pending",
                    "symbol": "DOGEUSDT",
                    "evidence_id": "EVID-999",
                }
            return None

        monkeypatch.setattr(paper_runtime, "get_latest_trade_approval", _fake_get_latest_trade_approval)

        send = await send_and_capture(adapter, "APROBAR EVID-321", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "La aprobacion pendiente ligada es TRADE-321" in response_text
        assert "APROBAR TRADE-321" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_trade_status_symbol_does_not_surface_consumed_approval_as_current(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded
        import tools.binance_paper_runtime as paper_runtime

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(paper_runtime, "get_latest_doge_premium_analysis_request", lambda **kwargs: None)

        def _fake_get_latest_trade_approval(**kwargs):
            status = kwargs.get("status")
            if status in {"pending", "approved"}:
                return None
            return {
                "approval_id": "TRADE-OLD321",
                "status": "consumed",
                "symbol": "DOGEUSDT",
                "proposal": {
                    "symbol": "DOGEUSDT",
                    "side": "BUY",
                    "notional_usd": "5.25",
                    "stop_loss_pct": "0.5",
                    "take_profit_pct": "1.0",
                },
                "market_summary": "tesis vieja que ya no debe salir como vigente",
            }

        monkeypatch.setattr(paper_runtime, "get_latest_trade_approval", _fake_get_latest_trade_approval)

        send = await send_and_capture(adapter, "ESTADO DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "No hay una aprobacion vigente para DOGE." in response_text
        assert "TRADE-OLD321 (ejecutada)" in response_text
        assert "tesis vieja" not in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_premium_analysis_symbol_bypasses_agent_loop(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_resolve_doge_premium_analysis_request",
            lambda **kwargs: {
                "success": True,
                "premium_outcome": "passed",
                "symbol": "DOGEUSDT",
                "request": {
                    "status": "completed",
                    "symbol": "DOGEUSDT",
                    "request_kind": "adjustment",
                    "model": "gemini-3.5-flash",
                    "material_payload": {
                        "position": {"approval_id": "TRADE-123", "market_price": "0.10080"},
                        "adjustment_context": {
                            "summary": "subir SL para asegurar beneficio",
                            "current_stop_price": "0.09950",
                            "current_take_profit_price": "0.10100",
                            "suggested_stop_price": "0.10020",
                            "suggested_take_profit_price": "0.10120",
                            "unrealized_pnl_usd": "0.40",
                            "unrealized_pnl_pct": "0.80",
                            "high_risk": True,
                            "high_risk_reason": "amplia el riesgo real",
                        },
                    },
                },
                "assessment": {
                    "confidence": "0.86",
                    "summary": "Gemini 3.5 Flash valida el ajuste.",
                    "risk_flags": ["volatilidad alta"],
                    "operator_note": "vigilar ejecucion",
                    "risk_label": "alto_riesgo",
                },
            },
        )

        send = await send_and_capture(adapter, "ANALIZAR DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Gemini 3.5 Flash valida el ajuste" in response_text
        assert "AJUSTAR DOGE" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_plaintext_premium_rejection_symbol_falls_back_to_lite_flow(self, adapter, runner, platform, monkeypatch):
        import agent.transports.binance_guarded_mcp_server as guarded

        monkeypatch.setattr(guarded, "_ensure_runtime_env_loaded", lambda: None)
        monkeypatch.setattr(
            guarded,
            "_resolve_doge_premium_analysis_request",
            lambda **kwargs: {
                "success": True,
                "premium_outcome": "denied_fallback",
                "symbol": "DOGEUSDT",
                "request": {
                    "status": "denied",
                    "symbol": "DOGEUSDT",
                    "request_kind": "entry",
                    "model": "gemini-3.5-flash",
                    "material_payload": {},
                },
                "trade_approval": {
                    "approval_id": "TRADE-999",
                    "expires_at": "2026-05-20T07:00:00+00:00",
                    "proposal": {
                        "symbol": "DOGEUSDT",
                        "side": "BUY",
                        "notional_usd": "5.25",
                        "stop_loss_pct": "0.5",
                        "take_profit_pct": "1.0",
                    },
                },
            },
        )

        send = await send_and_capture(adapter, "RECHAZAR ANALISIS DOGE", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "Gemini 3.1 Flash Lite" in response_text
        assert "Aprobacion requerida TRADE-999" in response_text
        runner._handle_message_with_agent.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_personality_lists_options(self, adapter, platform):
        send = await send_and_capture(adapter, "/personality", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "personalit" in response_text.lower()  # matches "personality" or "personalities"

    @pytest.mark.asyncio
    async def test_yolo_toggles_mode(self, adapter, platform):
        send = await send_and_capture(adapter, "/yolo", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "yolo" in response_text.lower()

    @pytest.mark.asyncio
    async def test_compress_command(self, adapter, platform):
        send = await send_and_capture(adapter, "/compress", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert "compress" in response_text.lower() or "context" in response_text.lower()

    @pytest.mark.asyncio
    async def test_quick_command_alias_targets_builtin_command_with_args(
        self, adapter, runner, platform
    ):
        """Alias targets with args must reach the built-in command handler."""
        runner.config.quick_commands = {
            "s": {"type": "alias", "target": "/status extra-arg"}
        }
        async def _handle_status(event):
            assert event.get_command_args() == "extra-arg"
            return "status via alias"

        runner._handle_status_command = AsyncMock(side_effect=_handle_status)

        send = await send_and_capture(adapter, "/s", platform)

        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        assert response_text == "status via alias"
        runner._handle_status_command.assert_awaited_once()
        runner._handle_message_with_agent.assert_not_awaited()



class TestSessionLifecycle:
    """Verify session state changes across command sequences."""

    @pytest.mark.asyncio
    async def test_new_then_status_reflects_reset(self, adapter, runner, session_entry, platform):
        """After /new, /status should report the fresh session."""
        await send_and_capture(adapter, "/new", platform)
        runner.session_store.reset_session.assert_called_once()

        send = await send_and_capture(adapter, "/status", platform)
        send.assert_called_once()
        response_text = send.call_args[1].get("content") or send.call_args[0][1]
        # Session ID from the entry should appear in the status output
        assert session_entry.session_id[:8] in response_text

    @pytest.mark.asyncio
    async def test_new_is_idempotent(self, adapter, runner, platform):
        """/new called twice should not crash."""
        await send_and_capture(adapter, "/new", platform)
        await send_and_capture(adapter, "/new", platform)
        assert runner.session_store.reset_session.call_count == 2


class TestAuthorization:
    """Verify the pipeline handles unauthorized users."""

    @pytest.mark.asyncio
    async def test_unauthorized_user_gets_pairing_response(self, adapter, runner, platform):
        """Unauthorized DM should trigger pairing code, not a command response."""
        runner._is_user_authorized = lambda _source: False

        event = make_event(platform, "/help")
        adapter.send.reset_mock()
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        # The adapter.send is called directly by the authorization path
        # (not via _send_with_retry), so check it was called with a pairing message
        adapter.send.assert_called()
        response_text = adapter.send.call_args[0][1] if len(adapter.send.call_args[0]) > 1 else ""
        assert "recognize" in response_text.lower() or "pair" in response_text.lower() or "ABC123" in response_text

    @pytest.mark.asyncio
    async def test_unauthorized_user_does_not_get_help(self, adapter, runner, platform):
        """Unauthorized user should NOT see the help command output."""
        runner._is_user_authorized = lambda _source: False

        event = make_event(platform, "/help")
        adapter.send.reset_mock()
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        # If send was called, it should NOT contain the help text
        if adapter.send.called:
            response_text = adapter.send.call_args[0][1] if len(adapter.send.call_args[0]) > 1 else ""
            assert "/new" not in response_text


class TestSendFailureResilience:
    """Verify the pipeline handles send failures gracefully."""

    @pytest.mark.asyncio
    async def test_send_failure_does_not_crash_pipeline(self, adapter, platform):
        """If send() returns failure, the pipeline should not raise."""
        adapter.send = AsyncMock(return_value=SendResult(success=False, error="network timeout"))
        adapter.set_message_handler(adapter._message_handler) # re-wire with same handler

        event = make_event(platform, "/help")
        # Should not raise — pipeline handles send failures internally
        await adapter.handle_message(event)
        await asyncio.sleep(0.3)

        adapter.send.assert_called()
