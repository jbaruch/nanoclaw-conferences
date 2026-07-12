---
name: nightly-cfp-sync
description: "Cadence wrapper that runs check-cfps on its own schedule: refresh open CFP data, apply Sessionize verification, update cfp-state.json, emit an observable-silence cursor marker. Triggers: 'cfp sync', 'sync cfps', 'nightly cfp sync', 'refresh cfps nightly'."
cadence: "30 6 * * * (TZ=local)"
agentModel: "claude-sonnet-4-6"
script: "scripts/precheck-nightly-cfp-sync.py"
evidence: "cfp-state.json#_last_checked"
---

# Nightly CFP Sync

Process steps in order. Do not skip ahead.

Run this wrapper silently. It consumes the inner skill's CFP list internally and surfaces only a stale-verification notice; the wrapper otherwise adds only cadence-cursor management and the observable-silence marker the silent-success watchdog reads from `task_run_logs.result`.

The fire-time precheck (`scripts/precheck-nightly-cfp-sync.py`) gates wake-ups by a filesystem cadence cap — the cap value and the wake/skip predicate are the script's contract (`CADENCE` constant). Design rationale in `references/cadence-rationale.md`.

## Step 1 — Refresh CFP data

Before anything else, capture the run-start instant once and hold it for Step 2's cursor gate:

```bash
date -u +%Y-%m-%dT%H:%M:%SZ
```

Hold this value as the run-start. Step 2 passes it to the cursor gate as `--since`, so capture it before invoking check-cfps below.

`Skill(skill: "tessl__check-cfps")` — refresh open CFP data, apply Sessionize verification, update `cfp-state.json`.

Consume the formatted CFP list internally — do not forward it to Baruch. The skill emits a machine-readable `<internal>` block at the end with `{checked_at, new_candidates_added, existing_verified, existing_verify_failed, verification}`. Parse that JSON. If `existing_verify_failed > 0`, forward a short notice via `mcp__nanoclaw__send_message` (e.g. `"check-cfps: <N> stored CFPs failed Sessionize verification this run; bot_notes prefixed with ⚠️ STALE DATA. Will retry next cycle."`). That notice drives the `surfaced` word in Step 3.

`verification` reports whether the run actually re-verified the Sessionize cohort (jbaruch/nanoclaw-conferences#8): `"live"` or `"none-required"` means it did (or had nothing to verify) — proceed normally. `"skipped"` means the freshness heartbeat did NOT advance (the verify driver was skipped or Sessionize was fully unreachable), so the run is NOT a clean success: notify Baruch via `mcp__nanoclaw__send_message` (e.g. `"check-cfps: Sessionize verification did not run this cycle (heartbeat held); will retry next fire."`), do NOT stamp the cursor (so the next cadence fire retries sooner instead of resting 72h on an unverified run), emit `<internal>nightly-cfp-sync exited: verify-skipped</internal>` as your final turn text, and finish here.

On complete *technical* failure (both primary sources unreachable), notify Baruch via `mcp__nanoclaw__send_message`, do NOT stamp the cursor — emit `<internal>nightly-cfp-sync exited: inner-skill-fail</internal>` as your final turn text and finish here. The next cadence fire retries.

## Step 2 — Advance the success cursor

Reachable only if Step 1 completed without a technical failure. Run the stamp script silently — its JSON stdout is internal bookkeeping; do NOT echo it or narrate "cursor stamped" / "run complete" to chat.

```bash
python3 /home/node/.claude/skills/tessl__nightly-cfp-sync/scripts/stamp-cursor.py --since "<run-start captured in Step 1>"
```

The stamp is **evidence-gated** (jbaruch/nanoclaw-conferences#49): it advances the cursor only when this run's verification is evidenced on the heartbeat, making the "don't rest the cadence on an unverified pass" guard deterministic rather than dependent on the agent honoring Step 1's verify-skipped branch. Required input: `--since` is the run-start captured in Step 1. The gate predicate is the script's (see `scripts/stamp-cursor.py` docstring). On a clean stamp it atomic-writes `/workspace/group/state/nightly-cfp-sync-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}` (the precheck reads `last_run` to gate the cadence).

Handle the exit code:

- **Exit 0** — cursor advanced. Stdout `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`. Proceed to Step 3.
- **Exit 3** — verification not evidenced on the heartbeat this run (driver skipped or Sessionize unreachable); the cursor did NOT advance, by design, so the next cadence fire retries sooner. Stdout `{"status": "skipped", "verification": "unevidenced", "reason": "<why>", "cursor_path": "<path>"}`. If Step 1 did not already send the heartbeat-held notice, send it via `mcp__nanoclaw__send_message`. Emit `<internal>nightly-cfp-sync exited: verify-skipped</internal>` as your final turn text and finish here.
- **Exit 2** — cursor write failure (diagnostic on stderr); the cursor did NOT advance. Do NOT emit the healthy Step 3 marker, which would falsely tell the silent-success watchdog the run completed. Emit `<internal>nightly-cfp-sync exited: cursor-stamp-fail</internal>` as your final turn text and finish here. The next cadence fire retries.

## Step 3 — Observable-silence marker

Your entire final turn is EXACTLY the one `<internal>` line below — no preceding prose, status report, or narration of Steps 1-2 (the optional Step 1 stale-verification notice was already sent separately). This is the marker the silent-success watchdog reads from `task_run_logs.result` to tell healthy quiet from broken-silently runs: `<internal>nightly-cfp-sync ran <slot_key>: clean</internal>` when Step 1 forwarded no stale-verification notice, or `<internal>nightly-cfp-sync ran <slot_key>: surfaced</internal>` when it did. `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. Finish here.
