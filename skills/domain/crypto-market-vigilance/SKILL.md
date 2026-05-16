---
name: crypto-market-vigilance
description: Use when Hermes should run a single non-trading crypto market watch pass, persist a local snapshot, and send a concise WhatsApp summary that can be scheduled via cronjob.
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [crypto, monitoring, whatsapp, cronjob, paper-first]
    related_skills: [hermes-agent]
---

# Crypto Market Vigilance

## Overview

This skill converts a raw loop-style market watcher into a single-pass Hermes workflow. The job is simple by design: collect a narrow set of market facts, persist them locally, and send a concise summary to the operator. It does not place trades, open Binance, or mutate exchange state.

The intended use is either:

- interactive status checks from WhatsApp, or
- scheduled cron runs that produce a local audit trail before you graduate to paper trading.

If the workspace already contains a helper script named `bucle_confianza.py` at the workspace root, prefer using it in one-shot mode rather than rebuilding the workflow from scratch. The script has been refactored to execute a single monitoring pass by default and only loops when `--loop` is supplied.

## When to Use

- You want a BTC, ETH, and SOL snapshot without opening any exchange state.
- You want an Excel trail or local ledger before introducing Binance execution.
- You want Hermes to send a WhatsApp report on a schedule.
- You want to validate gateway delivery, local model orchestration, or cron plumbing without taking market risk.

Do not use this skill for:

- placing orders,
- managing leverage or positions,
- deciding entries by itself,
- direct Binance automation.

## Procedure

1. Confirm the task is observation-only. If the request includes execution, stop and switch to the guarded Binance workflow instead.
2. Prefer the workspace helper script when available:

```bash
python bucle_confianza.py
```

3. Use this dry-run variant only when explicitly asked not to notify WhatsApp:

```bash
python bucle_confianza.py --no-whatsapp
```

4. If the helper script is unavailable, reproduce the same one-shot behavior manually:
- fetch BTC, ETH, SOL spot prices and BTC 24h volume from a public source,
- append the snapshot to the local workbook or ledger,
- send a short WhatsApp summary only after the local write succeeds.
5. Return a concise operator summary that includes timestamp, captured prices, ledger path, and whether delivery succeeded.

## Cronjob Recipe

Use this skill with a narrow cron configuration:

- `skills`: `crypto-market-vigilance`
- `enabled_toolsets`: `terminal`, `file`, `messaging`
- `workdir`: the workspace root that contains `bucle_confianza.py`
- `deliver`: `origin` when the cronjob is created from WhatsApp, otherwise the platform you want

Suggested prompt shape:

```text
Run one crypto market vigilance pass in the current workspace.
Use the helper script if it exists. Persist the local ledger first, then send a concise WhatsApp summary.
Do not place trades, open Binance, or modify any exchange state.
```

If you want an exact reusable prompt, use `references/cronjob-prompt.txt` from this skill.

## Operator Guardrails

- Keep this workflow read-only with respect to exchanges.
- Keep the watched symbols narrow until the ledger is stable.
- Treat messaging failures and write failures as separate outcomes; never report success when the workbook write failed.
- Do not silently change cadence or symbols in the cronjob without telling the user.

## Common Pitfalls

1. Leaving the helper script in infinite-loop mode. For Hermes cronjobs you want a single pass, not a daemon.
2. Sending WhatsApp before persisting the local ledger. If the local state is missing, the report is not auditable.
3. Mixing vigilance with trading logic. The monitoring path should remain boring and low-risk.
4. Giving the cronjob unnecessary toolsets like browser, terminal-wide admin actions, or exchange execution.

## Verification Checklist

- [ ] The job performs exactly one market-watch pass
- [ ] Local ledger or workbook write succeeds before message delivery
- [ ] WhatsApp summary is concise and deterministic
- [ ] No Binance or exchange-mutating action is invoked
- [ ] Cron configuration uses the narrowest viable toolsets