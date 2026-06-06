---
name: nightly-cfp-sync
description: "Cadence wrapper that runs check-cfps on its own schedule: refresh open CFP data, apply Sessionize verification, update cfp-state.json, emit an observable-silence cursor marker. Peeled off the nightly-external-sync bundle (jbaruch/nanoclaw#581) so the heavy full-cohort CFP verification gets its own bounded container. Triggers: 'cfp sync', 'sync cfps', 'nightly cfp sync', 'refresh cfps nightly'."
cadence: "30 6 * * * (TZ=local)"
script: "scripts/precheck-nightly-cfp-sync.py"
---

# Nightly CFP Sync

Process steps in order. Do not skip ahead.

Run this wrapper silently. It consumes the inner skill's CFP list internally and surfaces only a stale-verification notice; the wrapper otherwise adds only cadence-cursor management and the observable-silence marker the silent-success watchdog reads from `task_run_logs.result`.

The fire-time precheck (`scripts/precheck-nightly-cfp-sync.py`) gates wake-ups by a filesystem cadence cap — the cap value and the wake/skip predicate are the script's contract (`CADENCE` constant). Design rationale in `references/cadence-rationale.md`.

## Step 1 — Refresh CFP data

`Skill(skill: "tessl__check-cfps")` — refresh open CFP data, apply Sessionize verification, update `cfp-state.json`.

Consume the formatted CFP list internally — do not forward it to Baruch. The skill emits a machine-readable `<internal>` block at the end with `{checked_at, new_candidates_added, existing_verified, existing_verify_failed}`. Parse that JSON. If `existing_verify_failed > 0`, forward a short notice via `mcp__nanoclaw__send_message` (e.g. `"check-cfps: <N> stored CFPs failed Sessionize verification this run; bot_notes prefixed with ⚠️ STALE DATA. Will retry next cycle."`). That notice is the only user-facing output; it drives the `surfaced` word in Step 3.

On complete *technical* failure (both primary sources unreachable), notify Baruch via `mcp__nanoclaw__send_message`, do NOT stamp the cursor — emit `<internal>nightly-cfp-sync exited: inner-skill-fail</internal>` as your final turn text and finish here. The next cadence fire retries because the cursor stayed unadvanced.

## Step 2 — Advance the success cursor

Reachable only if Step 1 completed without a technical failure. Run the stamp script silently — its JSON stdout is internal bookkeeping; do NOT echo it or narrate "cursor stamped" / "run complete" to chat.

```bash
python3 /home/node/.claude/skills/tessl__nightly-cfp-sync/scripts/stamp-cursor.py
```

Atomic-writes `/workspace/group/state/nightly-cfp-sync-cursor.json` with `{"schema_version": 1, "last_run": "<now UTC ISO Z>"}`. The precheck reads `last_run` to gate the cadence (cap value in the script). Stdout: `{"status": "stamped", "last_run": "<iso>", "cursor_path": "<path>"}`. Proceed to Step 3.

## Step 3 — Observable-silence marker

Your entire final turn is EXACTLY the one `<internal>` line below — no preceding prose, status report, or narration of Steps 1-2 (the optional Step 1 stale-verification notice was already sent separately). This is the marker the silent-success watchdog reads from `task_run_logs.result` to tell healthy quiet from broken-silently runs: `<internal>nightly-cfp-sync ran <slot_key>: clean</internal>` when Step 1 forwarded no stale-verification notice, or `<internal>nightly-cfp-sync ran <slot_key>: surfaced</internal>` when it did. `<slot_key>` is today's UTC date in `YYYY-MM-DD` form. Finish here.
