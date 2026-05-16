"""Guarded Binance MCP server.

This server intentionally starts with a paper-first surface. Every trade
proposal is validated against the local risk policy before an execution
envelope is returned. Live order routing remains blocked until a dedicated
exchange adapter is added on top of this policy layer.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Optional

from tools.binance_guardrails import (
    BinanceAccountSnapshot,
    BinanceRiskLimits,
    BinanceTradeProposal,
    evaluate_trade_proposal,
    get_kill_switch_path,
    is_kill_switch_active,
    set_kill_switch,
)

logger = logging.getLogger(__name__)


def _build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:  # pragma: no cover - install hint
        raise ImportError(
            f"binance-guarded MCP server requires the 'mcp' package: {exc}"
        ) from exc

    mcp = FastMCP(
        "binance-guarded",
        instructions=(
            "Guarded Binance trading surface. Use it to validate candidate trades, "
            "inspect the active risk profile, and toggle a kill switch. This "
            "paper-first scaffold blocks live routing until a dedicated Binance "
            "adapter is explicitly added."
        ),
    )

    @mcp.tool()
    def binance_risk_profile() -> str:
        """Return the active Binance risk limits and kill switch state."""

        limits = BinanceRiskLimits.from_env()
        return json.dumps(
            {
                "success": True,
                "risk_profile": limits.to_dict(),
                "kill_switch_active": is_kill_switch_active(),
                "kill_switch_path": str(get_kill_switch_path()),
                "execution_mode": "paper-first",
            },
            indent=2,
        )

    @mcp.tool()
    def binance_set_kill_switch(enabled: bool, reason: str = "") -> str:
        """Toggle the local kill switch that blocks every trading action."""

        state = set_kill_switch(enabled=enabled, reason=reason)
        return json.dumps({"success": True, "kill_switch": state}, indent=2)

    @mcp.tool()
    def binance_validate_trade(
        symbol: str,
        side: str,
        notional_usd: float,
        mode: str = "paper",
        order_type: str = "MARKET",
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        leverage: float = 1.0,
        free_balance_usd: float = 0.0,
        open_positions: int = 0,
        positions_in_symbol: int = 0,
        daily_realized_pnl_usd: float = 0.0,
        verifier_model: str = "",
        verifier_passed: bool = False,
        verifier_confidence: float = 0.0,
        rationale: str = "",
        dry_run: bool = True,
    ) -> str:
        """Validate a trade proposal against the mandatory local risk policy."""

        proposal = BinanceTradeProposal.from_payload(
            {
                "symbol": symbol,
                "side": side,
                "notional_usd": notional_usd,
                "mode": mode,
                "order_type": order_type,
                "stop_loss_pct": stop_loss_pct or None,
                "take_profit_pct": take_profit_pct or None,
                "leverage": leverage,
                "verifier_model": verifier_model,
                "verifier_passed": verifier_passed,
                "verifier_confidence": verifier_confidence or None,
                "rationale": rationale,
                "dry_run": dry_run,
            }
        )
        account = BinanceAccountSnapshot.from_payload(
            {
                "free_balance_usd": free_balance_usd,
                "open_positions": open_positions,
                "positions_in_symbol": positions_in_symbol,
                "daily_realized_pnl_usd": daily_realized_pnl_usd,
                "kill_switch_active": is_kill_switch_active(),
            }
        )
        decision = evaluate_trade_proposal(
            proposal,
            account,
            BinanceRiskLimits.from_env(),
            kill_switch_active=is_kill_switch_active(),
        )
        return json.dumps({"success": True, "decision": decision.to_dict()}, indent=2)

    @mcp.tool()
    def binance_submit_trade(
        symbol: str,
        side: str,
        notional_usd: float,
        mode: str = "paper",
        order_type: str = "MARKET",
        stop_loss_pct: float = 0.0,
        take_profit_pct: float = 0.0,
        leverage: float = 1.0,
        free_balance_usd: float = 0.0,
        open_positions: int = 0,
        positions_in_symbol: int = 0,
        daily_realized_pnl_usd: float = 0.0,
        verifier_model: str = "",
        verifier_passed: bool = False,
        verifier_confidence: float = 0.0,
        rationale: str = "",
        dry_run: bool = True,
    ) -> str:
        """Return a dry-run execution envelope after the mandatory risk check."""

        decision_json = json.loads(
            binance_validate_trade(
                symbol=symbol,
                side=side,
                notional_usd=notional_usd,
                mode=mode,
                order_type=order_type,
                stop_loss_pct=stop_loss_pct,
                take_profit_pct=take_profit_pct,
                leverage=leverage,
                free_balance_usd=free_balance_usd,
                open_positions=open_positions,
                positions_in_symbol=positions_in_symbol,
                daily_realized_pnl_usd=daily_realized_pnl_usd,
                verifier_model=verifier_model,
                verifier_passed=verifier_passed,
                verifier_confidence=verifier_confidence,
                rationale=rationale,
                dry_run=dry_run,
            )
        )
        decision = decision_json["decision"]
        if not decision.get("allowed"):
            return json.dumps(
                {
                    "success": False,
                    "error": "trade rejected by risk guardrails",
                    "decision": decision,
                },
                indent=2,
            )

        requested_live = str(mode).strip().lower() == "live" and not dry_run
        if requested_live:
            return json.dumps(
                {
                    "success": False,
                    "error": (
                        "live execution is intentionally disabled in this scaffold. "
                        "Add a dedicated Binance adapter only after paper-trading "
                        "validation and operational review."
                    ),
                    "decision": decision,
                },
                indent=2,
            )

        return json.dumps(
            {
                "success": True,
                "execution_mode": "dry_run",
                "decision": decision,
                "order_preview": {
                    "symbol": symbol,
                    "side": side,
                    "order_type": order_type,
                    "mode": mode,
                    "notional_usd": notional_usd,
                    "rationale": rationale,
                },
            },
            indent=2,
        )

    return mcp


def main(argv: Optional[list[str]] = None) -> int:
    argv = argv or sys.argv[1:]
    verbose = "--verbose" in argv or "-v" in argv

    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        stream=sys.stderr,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    os.environ.setdefault("HERMES_QUIET", "1")
    os.environ.setdefault("HERMES_REDACT_SECRETS", "true")

    try:
        server = _build_server()
    except ImportError as exc:
        sys.stderr.write(f"binance-guarded MCP server cannot start: {exc}\n")
        return 2

    try:
        server.run()
    except KeyboardInterrupt:
        return 0
    except Exception as exc:  # pragma: no cover - integration surface
        logger.exception("binance-guarded MCP server crashed")
        sys.stderr.write(f"binance-guarded MCP server error: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())