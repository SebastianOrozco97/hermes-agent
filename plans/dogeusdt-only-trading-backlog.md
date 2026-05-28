# DOGEUSDT-Only Trading Implementation Backlog

## Objective

Align the current Hermes trading stack with a DOGEUSDT-only operating model:

- Phase 2 funding arbitrage is the primary strategy.
- Phase 3 grid is the range-bound secondary strategy.
- Phase 1 directional trading remains available as a small tactical overlay.
- WhatsApp approvals and guarded Binance execution remain mandatory.

## AI Model Stack

- Gemini 3.1 Flash-Lite is the primary AI model for the base operating flow.
- Gemini 3.5 Flash is the premium model used to corroborate higher-conviction investment opportunities.
- This backlog assumes that both roles remain in place unless the operating model is intentionally changed.

## Scope

In scope:

- DOGEUSDT spot plus perpetual workflows.
- Funding arbitrage hardening.
- Grid regime protection.
- Futures configuration enforcement.
- Macro-aware risk enforcement.
- Testing and operator-facing cleanup.

Out of scope for this backlog:

- DOGE/SHIB pairs trading.
- Quarterly futures or basis trading.
- Multi-symbol expansion.
- Replacing the WhatsApp approval flow.

## Execution Rules

- Do not start the next block until the current block has a narrow validation path.
- Any ticket that can leave partial market exposure must include rollback or recovery behavior.
- Prefer paper-safe tests and mocks before touching live paths.
- Changes must land in the real execution path, not only in helper modules.
- Preserve the current model split between the primary Gemini 3.1 Flash-Lite flow and the premium Gemini 3.5 Flash corroboration flow unless a ticket explicitly changes that architecture.

## Delivery Order

1. Block 1: Harden Phase 2 funding arbitrage.
2. Block 2: Enforce futures configuration deterministically.
3. Block 3: Protect Phase 3 grid against the wrong regime.
4. Block 4: Rebalance strategy hierarchy and connect macro to enforcement.
5. Block 5: Clean the operational surface and expand test coverage.

---

## Block 1 - Harden Phase 2 Funding Arbitrage

### B1-T1 - Reduce Arbitrage Default Leverage

Goal: move Phase 2 from a 5x default toward the conservative leverage profile required by the strategy thesis.

Files:

- `tools/doge_arbitrage_advisor.py`
- `agent/transports/binance_guarded_mcp_server.py`

Dependencies: none

Risk: high

Description:

Phase 2 currently defaults to leverage that is too aggressive for a DOGEUSDT-only funding strategy. Lower the default to 1x or 2x, and enforce the same value across planner, tool schema, and validation.

Acceptance criteria:

- Phase 2 default leverage is 1x or 2x in every exposed entry point.
- Requests above the configured strategy maximum are rejected before execution.
- Planner output, MCP input schema, and runtime guardrails stay consistent.

Checklist:

- [ ] Update the leverage default in the advisor.
- [ ] Update the leverage default in the guarded MCP surface.
- [ ] Add or tighten validation for leverage above the strategy cap.
- [ ] Add unit tests for default and rejection paths.

### B1-T2 - Fix Arbitrage Margin Transfer Sizing

Goal: correct the transfer sizing so spot, transfer amount, and futures notional are computed in consistent monetary units.

Files:

- `tools/execution_orchestrators.py`

Dependencies: B1-T1

Risk: high

Description:

The current transfer sizing appears to derive a transfer amount from asset quantity rather than from the actual USD or USDT margin requirement. Replace that logic with a deterministic monetary calculation tied to DOGEUSDT notional and target leverage.

Acceptance criteria:

- Transfer size is calculated in monetary terms rather than asset quantity shortcuts.
- The planned transfer amount matches the intended futures margin requirement.
- The same trade inputs produce deterministic spot size, transfer size, and futures notional.

Checklist:

- [ ] Replace the margin transfer formula.
- [ ] Document the sizing assumptions in code or adjacent tests.
- [ ] Add a numeric test case using a realistic DOGEUSDT example.
- [ ] Confirm the output stays stable across repeated runs.

### B1-T3 - Enforce Delta Neutrality Before Execution

Goal: block execution when the planned spot and futures legs are not close enough to delta neutral.

Files:

- `tools/arbitrage_guardrails.py`
- `tools/execution_orchestrators.py`

Dependencies: B1-T2

Risk: high

Description:

The codebase already contains a delta-neutrality helper, but the live arbitrage path does not appear to consume it. Move that check into the real execution path so the strategy cannot proceed with a materially imbalanced hedge.

Acceptance criteria:

- Real execution calls the neutrality check before placing legs.
- Execution aborts when hedge imbalance exceeds the configured threshold.
- Rejection details are surfaced clearly to the operator and logs.

Checklist:

- [ ] Wire the neutrality helper into the real arbitrage path.
- [ ] Define the rejection threshold and reason format.
- [ ] Add tests for pass and fail cases.
- [ ] Confirm that no legs are placed on a failed neutrality check.

### B1-T4 - Rework Arbitrage Sequencing With Rollback

Goal: make the Phase 2 execution sequence recoverable when one leg succeeds and a later step fails.

Files:

- `tools/execution_orchestrators.py`

Dependencies: B1-T3

Risk: high

Description:

Phase 2 currently executes spot purchase, transfer, and futures short in sequence without a robust compensating action model. Refactor the flow into explicit states and define rollback or recovery behavior for each failure point.

Acceptance criteria:

- The orchestration flow has explicit precheck, spot, transfer, futures, and post-check states.
- Failures after a successful earlier step trigger compensating logic or leave a recoverable state record.
- Operator output clearly reports whether the trade completed, compensated, or requires intervention.

Checklist:

- [ ] Refactor the execution sequence into named states.
- [ ] Define compensating actions for each failure boundary.
- [ ] Add tests for failure after spot buy and after transfer.
- [ ] Ensure the operator sees the exact partial-completion state.

### B1-T5 - Add Arbitrage Idempotency And Recovery State

Goal: prevent duplicate execution after process restarts or repeated approvals.

Files:

- `tools/execution_orchestrators.py`
- `agent/transports/binance_guarded_mcp_server.py`

Dependencies: B1-T4

Risk: high

Description:

Once rollback exists, the next requirement is idempotency. Record enough execution state to detect an in-progress or partially completed arbitrage and either recover or refuse to duplicate it.

Acceptance criteria:

- Each arbitrage attempt has a stable execution identifier.
- Re-running the same execution request does not duplicate completed legs.
- Recovery logic can resume or surface a clear intervention-needed state.

Checklist:

- [ ] Add an execution identifier to the arbitrage workflow.
- [ ] Persist minimal state needed for replay protection.
- [ ] Add a re-entry test after simulated interruption.
- [ ] Confirm duplicate approvals do not create duplicate exposure.

---

## Block 2 - Enforce Futures Configuration Deterministically

### B2-T1 - Impose Isolated Margin On Futures Paths

Goal: ensure futures operations do not depend on whatever margin mode the account happened to use previously.

Files:

- `tools/binance_live_adapter.py`

Dependencies: B1-T5

Risk: high

Description:

The strategy thesis expects explicit isolated margin behavior. Add an adapter path that sets the required margin type before sending futures orders and fails clearly if the exchange does not accept the change.

Acceptance criteria:

- Futures execution explicitly sets isolated margin before trading.
- Adapter behavior is deterministic for both already-isolated and newly-changed accounts.
- Failure to impose the required mode aborts the trade before order placement.

Checklist:

- [ ] Add a margin-type setter in the live adapter.
- [ ] Handle the exchange response for already-set margin mode.
- [ ] Surface a clear failure message if the mode cannot be enforced.
- [ ] Add focused tests around the new adapter path.

### B2-T2 - Impose Leverage Before Arbitrage And Grid Orders

Goal: eliminate hidden dependence on existing account leverage.

Files:

- `tools/execution_orchestrators.py`

Dependencies: B2-T1

Risk: medium-high

Description:

Both arbitrage and grid should set leverage explicitly before the first futures order. Do not assume the account is already configured correctly from a previous trade.

Acceptance criteria:

- Arbitrage sets leverage before opening the hedge.
- Grid sets leverage before seeding orders.
- Missing or failed leverage configuration blocks execution.

Checklist:

- [ ] Add explicit leverage setup to the arbitrage path.
- [ ] Add explicit leverage setup to the grid path.
- [ ] Return a clear error if leverage setup fails.
- [ ] Add tests for both execution branches.

### B2-T3 - Unify Strategy Leverage Guardrails

Goal: ensure the planner, guardrails, and executor use the same leverage policy.

Files:

- `tools/binance_guardrails.py`
- `tools/doge_arbitrage_advisor.py`

Dependencies: B2-T2

Risk: medium

Description:

The codebase currently mixes strategy defaults and runtime caps across multiple files. Move the leverage policy toward a single source of truth or a tightly synchronized model so Phase 2 cannot drift over time.

Acceptance criteria:

- A single strategy definition governs Phase 2 leverage limits.
- Planner defaults and runtime caps cannot disagree silently.
- Tests fail when a new mismatch is introduced.

Checklist:

- [ ] Choose the source of truth for strategy leverage caps.
- [ ] Align the planner with that source.
- [ ] Align runtime guardrails with that source.
- [ ] Add regression tests for mismatched configuration.

---

## Block 3 - Protect Phase 3 Grid Against The Wrong Regime

### B3-T1 - Add A Regime Filter Before Grid Activation

Goal: prevent grid activation in directional or breakout conditions.

Files:

- `tools/doge_grid_advisor.py`
- `hermes_home/scripts/doge_grid_scout.py`

Dependencies: B2-T3

Risk: medium

Description:

Grid trading should only run when DOGEUSDT is genuinely range-bound. Add a regime filter based on volatility structure, moving-average slope, and other simple diagnostics already compatible with the existing planner.

Acceptance criteria:

- Grid activation requires a positive lateral-regime check.
- Trending conditions prevent the grid from being proposed.
- Regime evaluation is visible in operator output and tests.

Checklist:

- [ ] Define the regime filter inputs.
- [ ] Implement the filter in the planner or scout.
- [ ] Expose the regime result to logs or approval messages.
- [ ] Add tests for lateral and trending cases.

### B3-T2 - Turn Grid Breakout Limits Into Executable Rules

Goal: make the planner's breakout bounds operational rather than informational.

Files:

- `tools/doge_grid_advisor.py`
- `tools/execution_orchestrators.py`

Dependencies: B3-T1

Risk: medium

Description:

The planner already computes range boundaries and stop levels, but the execution layer should act on them. Add cancellation and protection behavior tied directly to those limits.

Acceptance criteria:

- Grid execution watches the planner's breakout boundaries.
- Orders are cancelled or frozen when the market exits the allowed range.
- Operator output states whether the grid stopped due to breakout protection.

Checklist:

- [ ] Pass the breakout limits from planning into execution.
- [ ] Define the cancellation or freeze behavior.
- [ ] Add tests for upper and lower boundary breaches.
- [ ] Confirm that the grid does not continue seeding after breakout.

### B3-T3 - Define Residual Inventory Exit And Re-entry Rules

Goal: prevent the grid from leaving unmanaged inventory after a range break.

Files:

- `tools/execution_orchestrators.py`

Dependencies: B3-T2

Risk: medium

Description:

If the grid has already accumulated inventory when the range breaks, the system needs a deterministic policy for inventory exit or freeze, plus a rule for when re-entry becomes allowed again.

Acceptance criteria:

- Residual inventory handling is explicitly defined for breakout scenarios.
- Automatic re-entry remains disabled until the regime is revalidated.
- Operator output distinguishes between a stopped grid and a resumed grid.

Checklist:

- [ ] Define the residual inventory policy.
- [ ] Define the re-entry gate.
- [ ] Add tests for breakout while inventory exists.
- [ ] Ensure the state model supports a stopped grid status.

---

## Block 4 - Rebalance Strategy Hierarchy And Connect Macro To Enforcement

### B4-T1 - Reduce Phase 1 To A Tactical Overlay

Goal: make sure directional trading does not dominate the DOGEUSDT-only strategy stack.

Files:

- `hermes_home/scripts/doge_live_scout.py`
- `tools/binance_guardrails.py`

Dependencies: B3-T3

Risk: medium

Description:

Phase 1 should remain available, but with tighter notional, exposure, or frequency limits than the core funding and grid strategies. The change is architectural, not cosmetic.

Acceptance criteria:

- Phase 1 has lower exposure limits than the primary strategy stack.
- The limit is enforced in the real path, not only in messaging.
- Operator output makes the tactical nature of Phase 1 visible.

Checklist:

- [ ] Define the tactical overlay limits.
- [ ] Enforce them through guardrails or execution policy.
- [ ] Reflect them in operator-facing messages.
- [ ] Add tests for exposure cap enforcement.

### B4-T2 - Move Macro State Into Real Sizing And Approval Logic

Goal: make macro context change the actual risk decision, not only the premium-analysis payload.

Files:

- `tools/macro_data_oracle.py`
- `tools/binance_guardrails.py`
- `hermes_home/scripts/doge_live_scout.py`

Dependencies: B4-T1

Risk: medium

Description:

Macro context is already available, but it needs to alter live approval and sizing behavior. Wire macro state into the real guardrail path so adverse conditions reduce size or block entries.

Acceptance criteria:

- Macro input reaches the live guardrail decision.
- Adverse macro conditions reduce notional or block execution based on policy.
- Tests cover at least one reduced-size case and one blocked case.

Checklist:

- [ ] Route macro state into the real risk evaluation path.
- [ ] Define the reduction and block thresholds.
- [ ] Update operator output to show macro-driven changes.
- [ ] Add focused tests for reduced and blocked outcomes.

### B4-T3 - Make Strategy Priority Explicit In Config And Messaging

Goal: avoid ambiguity about which strategy is primary and which one is tactical.

Files:

- `hermes_home/scripts/doge_live_scout.py`
- `agent/transports/binance_guarded_mcp_server.py`

Dependencies: B4-T2

Risk: low-medium

Description:

Even after the hard limits are updated, the system should communicate strategy hierarchy clearly. Explicit operator messaging reduces approval mistakes and future drift.

Acceptance criteria:

- Config or runtime labels clearly distinguish core and tactical strategies.
- Approval messages reflect the correct hierarchy.
- Internal naming does not imply that the directional path is the primary engine.

Checklist:

- [ ] Review strategy labels exposed to the operator.
- [ ] Update approval and summary messages where needed.
- [ ] Align internal naming if it is materially misleading.
- [ ] Add a regression check for the updated messaging.

---

## Block 5 - Clean The Operational Surface And Expand Test Coverage

### B5-T1 - Remove Duplicate MCP Definitions For Arbitrage And Grid

Goal: eliminate functional drift caused by duplicate tool definitions.

Files:

- `agent/transports/binance_guarded_mcp_server.py`

Dependencies: B4-T3

Risk: medium

Description:

The guarded Binance MCP surface contains duplicate definitions for arbitrage and grid. Collapse them to a single source per tool so behavior cannot drift across duplicated code blocks.

Acceptance criteria:

- Each arbitrage and grid tool is defined once.
- Tool behavior remains unchanged except for the intended cleanup.
- Search-based regression checks show no duplicate definitions remain.

Checklist:

- [ ] Identify the surviving definition for each tool.
- [ ] Remove duplicate implementations.
- [ ] Re-run targeted searches for duplicate function names.
- [ ] Smoke-test tool invocation paths.

### B5-T2 - Repair Broken Scout Fallback Paths

Goal: prevent development or degraded-mode execution from failing on invalid fallback construction.

Files:

- `hermes_home/scripts/doge_arbitrage_scout.py`
- `hermes_home/scripts/doge_grid_scout.py`

Dependencies: B5-T1

Risk: medium

Description:

Some scout fallbacks instantiate futures executors with invalid arguments. Fix the fallback path so degraded mode remains usable and predictable.

Acceptance criteria:

- Arbitrage scout fallback initializes correctly.
- Grid scout fallback initializes correctly.
- Fallback behavior is covered by at least one narrow test or controlled execution check.

Checklist:

- [ ] Fix invalid fallback executor construction in the arbitrage scout.
- [ ] Fix invalid fallback executor construction in the grid scout.
- [ ] Add narrow tests or execution checks for both paths.
- [ ] Confirm errors are operator-readable if fallback still cannot proceed.

### B5-T3 - Correct Operator Messaging Units In Arbitrage Scout

Goal: stop showing DOGE quantities as if they were USD amounts.

Files:

- `hermes_home/scripts/doge_arbitrage_scout.py`

Dependencies: B5-T2

Risk: low

Description:

Operator-facing messages should distinguish DOGE quantity from USD or USDT value. Mislabeling is not just cosmetic when approvals depend on those numbers.

Acceptance criteria:

- Approval messages label DOGE quantity and USDT value correctly.
- No message field shows asset quantity as a currency amount.
- Tests or snapshot checks cover the corrected message.

Checklist:

- [ ] Review the arbitrage approval message fields.
- [ ] Update the wording and value mapping.
- [ ] Add a narrow regression check for the message.
- [ ] Confirm the message remains concise for WhatsApp.

### B5-T4 - Remove Duplicate `macro_state` Keys From Premium Payloads

Goal: clean low-risk technical debt in the premium analysis payload builder.

Files:

- `tools/doge_premium_flow.py`

Dependencies: B5-T1

Risk: low

Description:

The premium payload currently repeats the same `macro_state` key multiple times. Remove the duplication so the builder is deterministic and easier to trust.

Acceptance criteria:

- Entry payload includes a single `macro_state` key.
- Adjustment payload includes a single `macro_state` key.
- Tests confirm the serialized payload shape.

Checklist:

- [ ] Remove duplicate `macro_state` entries from entry payloads.
- [ ] Remove duplicate `macro_state` entries from adjustment payloads.
- [ ] Add or update a payload-shape test.
- [ ] Confirm no downstream code expects duplicated keys.

### B5-T5 - Add Targeted Test Coverage For Arbitrage And Grid

Goal: stop relying on live adapter tests alone for the highest-risk strategy paths.

Files:

- `tests/`
- `tools/execution_orchestrators.py`
- `tools/doge_arbitrage_advisor.py`
- `tools/doge_grid_advisor.py`

Dependencies: B1-T5, B2-T3, B3-T3

Risk: high

Description:

Arbitrage and grid need their own focused tests. Cover the main economic and operational failure modes introduced by Blocks 1 through 3.

Acceptance criteria:

- Arbitrage tests cover sizing, neutrality, rollback, and replay protection.
- Grid tests cover regime filtering, breakout cancellation, and residual inventory handling.
- The new test suite can run independently from live exchange credentials.

Checklist:

- [ ] Add arbitrage planner tests.
- [ ] Add arbitrage execution tests.
- [ ] Add grid planner tests.
- [ ] Add grid execution tests.
- [ ] Add a documented narrow test command for these suites.

---

## Suggested Execution Sequence

1. B1-T1
2. B1-T2
3. B1-T3
4. B1-T4
5. B1-T5
6. B2-T1
7. B2-T2
8. B2-T3
9. B3-T1
10. B3-T2
11. B3-T3
12. B4-T1
13. B4-T2
14. B4-T3
15. B5-T1
16. B5-T2
17. B5-T3
18. B5-T4
19. B5-T5

## Definition Of Done

A ticket is complete only when all of the following are true:

- The live or paper execution path uses the new logic directly.
- A narrow automated test or focused executable validation exists.
- Operator output remains understandable after the change.
- The change does not introduce a new hidden dependency on exchange account state.