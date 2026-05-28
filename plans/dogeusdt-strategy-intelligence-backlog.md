# DOGEUSDT Strategy and Intelligence Backlog

## Objective

Evolve the current Hermes DOGEUSDT trading stack from a safe strategy-specific executor into a selective, evidence-driven operator that:

- compares tactical overlay, funding arbitrage, grid, and explicit no-trade outcomes in one decision layer;
- records enough context to learn which ideas work by regime and which do not;
- sizes capital dynamically instead of relying on mostly fixed notionals;
- uses Gemini as a structured proposer plus challenger pair rather than a single confirmer.

This backlog assumes that the current critical execution fixes are already closed and that the focused Binance and DOGE regression slice remains green.

## AI Model Stack

- Gemini 3.1 Flash-Lite remains the primary model for the base DOGE operating flow.
- Gemini 3.5 Flash remains the premium model for higher-conviction review, but its role expands from corroboration into structured challenge.
- Deterministic market metrics, guardrails, approvals, and exchange validation remain mandatory. No model can bypass them.

## Scope

In scope:

- DOGEUSDT-only meta-selection across overlay, arbitrage, grid, and no-trade.
- Decision journaling and outcome attribution.
- Regime-aware scorecards and selector feedback.
- Dynamic sizing and strategy budgeting.
- Adversarial AI review, shadow mode, and promotion gates.

Out of scope for this backlog:

- Multi-symbol expansion.
- Fully autonomous live trading without operator approval.
- Reinforcement learning, fine-tuning, or custom model training.
- Replacing the existing guarded Binance execution surface.

## Execution Rules

- Do not add intelligence that only exists in notebooks or reports; every decision path must land in the real runtime.
- Every strategy comparison must support an explicit no-trade outcome.
- New model-driven logic must persist both rationale and disagreement, not only the final answer.
- Scorecards must be derived from durable journal data, not from ephemeral logs.
- Dynamic sizing must remain capped by existing Binance guardrails and operator approval rules.
- Promote new decision logic from replay to paper/shadow before allowing it to influence live operator recommendations.

## Delivery Order

1. Block 1: Build a DOGE strategy selector.
2. Block 2: Capture decision context and outcome attribution.
3. Block 3: Score strategies by regime and feed that back into selection.
4. Block 4: Replace mostly fixed sizing with dynamic allocation.
5. Block 5: Add adversarial AI review and promotion gates.

---

## Block 1 - Build a DOGE Strategy Selector

### B1-T1 - Normalize Opportunity Payloads Across Strategies

Goal: make overlay, arbitrage, grid, and no-trade comparable in one schema.

Files:

- `tools/doge_strategy_selector.py` (new)
- `tools/doge_signal_engine.py`
- `tools/doge_arbitrage_advisor.py`
- `tools/doge_grid_advisor.py`

Dependencies: none

Risk: high

Description:

Today each strategy speaks its own output shape. Introduce a shared `StrategyOpportunity` contract with fields such as `strategy_id`, `eligible`, `blockers`, `expected_edge`, `confidence`, `capital_required_usd`, `holding_horizon`, `macro_alignment`, `regime_tags`, and `operator_summary`.

Acceptance criteria:

- Overlay, arbitrage, and grid each emit a normalized opportunity payload.
- Ineligible strategies emit explicit blockers instead of disappearing silently.
- The selector contract supports an explicit `no_trade` payload.

Checklist:

- [ ] Add a normalized opportunity dataclass or equivalent schema.
- [ ] Map overlay output into the shared contract.
- [ ] Map arbitrage output into the shared contract.
- [ ] Map grid output into the shared contract.
- [ ] Add tests for eligible and blocked payloads per strategy.

### B1-T2 - Add a Deterministic Meta-Selector

Goal: choose one DOGE action path, or abstain, from the competing strategy opportunities.

Files:

- `tools/doge_strategy_selector.py` (new)
- `hermes_home/scripts/doge_live_scout.py`
- `hermes_home/scripts/doge_arbitrage_scout.py`
- `hermes_home/scripts/doge_grid_scout.py`

Dependencies: B1-T1

Risk: high

Description:

Implement a deterministic priority policy that compares strategy opportunities instead of running three independent recommendation lanes. The selector must be able to choose overlay, arbitrage, grid, or no-trade, while preserving the current guarded execution model.

Acceptance criteria:

- The selector returns one winner or `no_trade`.
- Macro blockers, execution blockers, and sample-size blockers can force abstention.
- The selector returns both the chosen strategy and the ranked rejected alternatives.

Checklist:

- [ ] Define ranking inputs and tie-break rules.
- [ ] Fail closed to `no_trade` when evidence is conflicting or incomplete.
- [ ] Surface ranked alternatives in the result payload.
- [ ] Add tests for overlay vs grid, arbitrage vs overlay, and abstention cases.

### B1-T3 - Replace Parallel Human-Facing Recommendations With One Priority Digest

Goal: give the operator one primary DOGE recommendation instead of three loosely related scout outputs.

Files:

- `hermes_home/scripts/doge_strategy_router.py` (new)
- `hermes_home/cron/jobs.json`
- `hermes_home/scripts/doge_live_scout.py`
- `hermes_home/scripts/doge_arbitrage_scout.py`
- `hermes_home/scripts/doge_grid_scout.py`

Dependencies: B1-T2

Risk: medium-high

Description:

Keep the underlying scouts as sources, but introduce a single top-level DOGE strategy digest that consumes the normalized opportunities and publishes the prioritized action for the current regime.

Acceptance criteria:

- The operator receives one primary DOGE strategy digest for the active cycle.
- The digest names the selected strategy, rejected alternatives, and the reason for abstention when applicable.
- Existing strategy-specific scouts can remain available as diagnostics but do not represent the top-level recommendation anymore.

Checklist:

- [ ] Add a top-level DOGE strategy router script.
- [ ] Wire the router to the selector output.
- [ ] Keep diagnostic access to the underlying scouts.
- [ ] Add a focused test for operator-facing digest formatting.

---

## Block 2 - Capture Decision Context and Outcome Attribution

### B2-T1 - Persist Rich Entry Decision Context

Goal: record enough information at entry time to audit and later learn from each trade idea.

Files:

- `tools/binance_paper_runtime.py`
- `agent/transports/binance_guarded_mcp_server.py`
- `tools/doge_strategy_selector.py`

Dependencies: B1-T3

Risk: high

Description:

The current journal captures events and PnL, but not a full decision snapshot. Extend entry records to persist the selected strategy, rejected alternatives, regime labels, macro state, verifier assessments, expected edge, and sizing rationale.

Acceptance criteria:

- Every paper and live proposal/entry records the chosen strategy and the alternatives considered.
- The journal captures macro state, signal snapshots, model assessments, expected edge, and planned hold horizon.
- Entry records remain readable and backward-compatible enough for existing summaries.

Checklist:

- [ ] Extend the journal schema for entry records.
- [ ] Persist the selector output and rationale.
- [ ] Persist the model verdicts and confidence values.
- [ ] Add tests for the new entry journal payload shape.

### B2-T2 - Persist Exit Attribution And Thesis Outcome

Goal: close the loop between idea, execution, and result.

Files:

- `tools/binance_paper_runtime.py`
- `tools/execution_orchestrators.py`
- `tools/doge_live_manager.py`

Dependencies: B2-T1

Risk: high

Description:

Add structured exit attribution so the journal can answer not just how much was won or lost, but why. Capture whether the trade won because the thesis worked, because volatility was favorable, because the stop was too loose, because protection was tightened early, or because execution degraded the idea.

Acceptance criteria:

- Closed positions include thesis outcome tags and trigger categories.
- Exit records compare expected vs realized outcome fields where available.
- Management adjustments and breakout exits leave enough context to audit whether the strategy or execution was at fault.

Checklist:

- [ ] Add thesis outcome and failure-mode fields to close records.
- [ ] Persist execution and protection-adjustment context on exit.
- [ ] Add tests for close-event attribution payloads.
- [ ] Ensure summaries still render without the new fields being mandatory for old records.

### B2-T3 - Add Query Helpers For Strategy And Regime History

Goal: make the stored journal data usable without offline manual parsing.

Files:

- `tools/binance_paper_runtime.py`
- `tools/doge_strategy_scorecard.py` (new)

Dependencies: B2-T2

Risk: medium-high

Description:

The journal should support queries like "show overlay results in bearish macro" or "show DOGE grid outcomes in quiet range regimes over the last 14 days". Add helper functions and compact summaries that downstream tools can consume.

Acceptance criteria:

- Journal helpers can filter by strategy, regime, date range, and outcome.
- Daily and weekly summaries can consume the new helpers.
- Query helpers are deterministic and covered by tests.

Checklist:

- [ ] Add journal filters for strategy and regime.
- [ ] Add compact helper outputs for scorecards.
- [ ] Add tests for filter combinations.
- [ ] Keep the helper API stable enough for cron summaries.

---

## Block 3 - Score Strategies By Regime And Feed Back Into Selection

### B3-T1 - Build A Stable DOGE Regime Taxonomy

Goal: label the market in a way that can be reused by selection, journaling, and scorecards.

Files:

- `tools/doge_regime_classifier.py` (new)
- `tools/doge_signal_engine.py`
- `tools/macro_data_oracle.py`

Dependencies: B2-T3

Risk: medium-high

Description:

Current logic uses separate concepts such as score, ATR ratio, trend bias, and macro alignment. Consolidate those into a stable regime taxonomy such as `breakout_trend`, `quiet_range`, `high_volatility_stress`, `funding_rich_carry`, and `macro_divergent_chop`.

Acceptance criteria:

- Every opportunity and closed trade can be assigned one stable regime label.
- Regime labeling uses deterministic metrics already available in runtime.
- Regime labels are available to the selector and the journal.

Checklist:

- [ ] Define the initial DOGE regime taxonomy.
- [ ] Implement a regime classifier from existing metrics.
- [ ] Feed the label into strategy opportunity payloads.
- [ ] Add tests for at least one example per regime bucket.

### B3-T2 - Compute Scorecards By Strategy x Regime

Goal: measure where each strategy actually has edge.

Files:

- `tools/doge_strategy_scorecard.py` (new)
- `tools/binance_paper_runtime.py`
- `hermes_home/scripts/binance_paper_daily_summary.py`

Dependencies: B3-T1

Risk: high

Description:

Build scorecards that go beyond PnL totals. The minimum set should include sample count, approval conversion, hit rate, expectancy, median hold time, median drawdown proxy, and realized PnL by strategy and regime.

Acceptance criteria:

- Scorecards can be computed for strategy, regime, and strategy x regime slices.
- Scorecards expose at least expectancy, hit rate, realized PnL, and sample count.
- Operator summaries can reference the scorecards without manual data crunching.

Checklist:

- [ ] Implement strategy-level scorecards.
- [ ] Implement regime-level scorecards.
- [ ] Implement strategy x regime scorecards.
- [ ] Add tests using synthetic journal records.

### B3-T3 - Feed Historical Score Back Into The Selector

Goal: allow recent evidence to change strategy ranking rather than keeping the selector purely static.

Files:

- `tools/doge_strategy_selector.py`
- `tools/doge_strategy_scorecard.py`

Dependencies: B3-T2

Risk: high

Description:

Once scorecards exist, the selector should stop treating every eligible strategy as equally credible. Add a deterministic feedback policy that can up-rank, down-rank, or abstain based on recent regime-adjusted performance and minimum sample requirements.

Acceptance criteria:

- Selector ranking incorporates scorecard evidence.
- Small-sample regimes do not overfit into aggressive weighting.
- Persistent poor performance can downgrade a strategy to diagnostic-only or no-trade.

Checklist:

- [ ] Define minimum sample thresholds.
- [ ] Add ranking penalties and bonuses from scorecards.
- [ ] Add abstention behavior for weak or negative evidence.
- [ ] Add tests for strong-edge, weak-edge, and insufficient-sample cases.

### B3-T4 - Calibrate Signal And Model Confidence Against Outcomes

Goal: stop treating heuristic scores and Gemini confidence as if they were already calibrated probabilities.

Files:

- `tools/doge_signal_engine.py`
- `tools/doge_gemini_verifier.py`
- `tools/doge_strategy_scorecard.py`

Dependencies: B3-T2

Risk: medium-high

Description:

The current signal score and verifier confidence are useful, but static. Add calibration reports that show what actually happened when signal score was 5, 6, or 7, and when Gemini confidence clustered in different bands.

Acceptance criteria:

- Calibration outputs exist for signal score bands and model confidence bands.
- Selector and sizing can consume the calibrated bands rather than raw confidence alone.
- Reports stay deterministic and reproducible from the journal.

Checklist:

- [ ] Add confidence-band aggregation helpers.
- [ ] Add signal-score outcome aggregation helpers.
- [ ] Expose a compact calibration summary.
- [ ] Add tests for the aggregation math.

---

## Block 4 - Replace Mostly Fixed Sizing With Dynamic Allocation

### B4-T1 - Add A Deterministic DOGE Position Sizer

Goal: compute notional from evidence quality instead of mainly from static env defaults.

Files:

- `tools/doge_position_sizer.py` (new)
- `hermes_home/scripts/doge_live_scout.py`
- `hermes_home/scripts/doge_arbitrage_scout.py`
- `hermes_home/scripts/doge_grid_scout.py`

Dependencies: B3-T4

Risk: high

Description:

Introduce a sizing policy that converts strategy rank, calibrated confidence, regime score, and execution quality into a recommended notional. This policy must still sit beneath existing risk caps and approval rules.

Acceptance criteria:

- Position size is computed from a sizing policy, not only from one fixed default env var.
- The sizing output shows the components that raised or reduced size.
- Guardrail caps still bound the final notional.

Checklist:

- [ ] Define the sizing inputs and formula.
- [ ] Add a transparent sizing explanation payload.
- [ ] Wire the sizer into overlay, arbitrage, and grid recommendation flows.
- [ ] Add tests for large, reduced, and zero-size outcomes.

### B4-T2 - Add Strategy Budgets, Cooldowns, And Exposure Sharing

Goal: manage capital across strategies instead of evaluating each one in isolation.

Files:

- `tools/binance_guardrails.py`
- `tools/doge_position_sizer.py`
- `tools/binance_paper_runtime.py`

Dependencies: B4-T1

Risk: high

Description:

Add per-strategy budget limits, cooldown rules after losses or poor scorecard performance, and basic exposure sharing so one strategy can consume the budget that another one should not use.

Acceptance criteria:

- Overlay, arbitrage, and grid each have explicit budget policies.
- Repeated poor outcomes can trigger strategy-specific cooldowns.
- Selector and sizing can reject a valid idea because the relevant budget or cooldown state says no.

Checklist:

- [ ] Define per-strategy budgets.
- [ ] Define loss-streak or quality-based cooldown rules.
- [ ] Persist budget and cooldown state where needed.
- [ ] Add tests for exhausted-budget and cooldown scenarios.

### B4-T3 - Penalize Low-Quality Execution Surfaces Before Entry

Goal: reduce size or abstain when the exchange surface is hostile even if the idea looks good.

Files:

- `tools/binance_live_adapter.py`
- `tools/execution_orchestrators.py`
- `tools/doge_position_sizer.py`

Dependencies: B4-T1

Risk: medium-high

Description:

Execution quality should influence whether the trade is worth taking and at what size. Add penalties for coarse quantity steps, poor min-notional fit, projected slippage, or unfavorable liquidity conditions that meaningfully degrade the setup.

Acceptance criteria:

- Execution-quality penalties can reduce size or force no-trade.
- The penalty decision is visible in the operator-facing sizing explanation.
- Tests cover at least one coarse-step DOGE case and one slippage penalty case.

Checklist:

- [ ] Define execution-quality penalty inputs.
- [ ] Add penalties for low-quality exchange conditions.
- [ ] Surface the penalties in the sizing output.
- [ ] Add focused tests around reduced-size and reject paths.

---

## Block 5 - Add Adversarial AI Review And Promotion Gates

### B5-T1 - Split Proposer And Challenger Roles

Goal: make the AI review layer attack the trade thesis, not only confirm it.

Files:

- `tools/doge_gemini_verifier.py`
- `tools/doge_premium_flow.py`
- `tools/doge_decision_review.py` (new)

Dependencies: B4-T3

Risk: medium-high

Description:

The base model should still summarize the setup, but a second pass should explicitly search for why not to trade, what evidence is missing, what invalidates the setup, and whether the trade should be reduced or vetoed.

Acceptance criteria:

- The review flow produces both a proposer and a challenger output.
- Challenger output uses a structured schema, not freeform prose only.
- The review flow remains deterministic enough to test with stubs and fixed payloads.

Checklist:

- [ ] Define proposer and challenger payload schemas.
- [ ] Add a challenger prompt focused on refutation.
- [ ] Persist both outputs in the decision journal.
- [ ] Add tests for parsed challenger responses.

### B5-T2 - Add A Disagreement Policy And Operator Surface

Goal: convert model disagreement into explicit operating rules.

Files:

- `hermes_home/scripts/doge_live_scout.py`
- `agent/transports/binance_guarded_mcp_server.py`
- `tools/doge_decision_review.py` (new)

Dependencies: B5-T1

Risk: high

Description:

Disagreement between proposer and challenger should not be hand-waved. Define when disagreement means no-trade, when it means reduced size, and when it means paper-only or premium-only review.

Acceptance criteria:

- Strong challenger veto can force no-trade.
- Moderate disagreement can downshift size or require premium escalation.
- Operator messages show both the supporting and opposing thesis in compact form.

Checklist:

- [ ] Define the disagreement policy tiers.
- [ ] Wire disagreement outcomes into selector and sizing decisions.
- [ ] Update operator messaging to show support vs challenge.
- [ ] Add tests for veto, downsize, and escalate outcomes.

### B5-T3 - Build Replay, Shadow Mode, And Promotion Gates

Goal: make strategy upgrades prove themselves before they influence live recommendations.

Files:

- `tools/doge_strategy_replay.py` (new)
- `tools/doge_strategy_selector.py`
- `tools/doge_strategy_scorecard.py`
- `hermes_home/scripts/doge_strategy_router.py`

Dependencies: B5-T2

Risk: high

Description:

Add a replay and shadow harness that can re-run historical or near-real-time data through the selector without executing orders. Define promotion thresholds for moving a new selector or sizing policy from replay to paper/shadow and then to live recommendation influence.

Acceptance criteria:

- Historical replay can evaluate selector outputs without touching execution.
- Shadow mode logs what the new selector would have done next to what the current flow actually did.
- Promotion gates are explicit and based on scorecard evidence, not intuition.

Checklist:

- [ ] Add a replay tool over journal plus market inputs.
- [ ] Add a shadow mode result channel.
- [ ] Define promotion thresholds and rollback triggers.
- [ ] Add tests for replay and shadow result serialization.
