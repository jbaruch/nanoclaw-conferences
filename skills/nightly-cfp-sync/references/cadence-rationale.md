# Cadence rationale — why a 3-day cap

This wrapper was peeled off `nightly-external-sync` (`jbaruch/nanoclaw#581`) so the heavy check-cfps run gets its own bounded container. Inside the bundle, check-cfps was Step 8 — the long tail (full-cohort Sessionize verification across every stored `open`/`approved` entry, plus web-search gap discovery) that exhausted the run before the wrapper could reach its final summary, leaving `task_run_logs.result` empty.

## Chosen — 3-day filesystem cadence cap

Precheck reads `/workspace/group/state/nightly-cfp-sync-cursor.json`. If `last_run` is missing or older than `CADENCE = 3d`, wake; otherwise skip. This preserves the effective cadence CFP refresh ran at inside the bundle (the bundle's own cap was 3 days) — CFP deadlines move on a multi-day horizon, so a 3-day refresh keeps `cfp-state.json` current without paying the verification cost daily.

## Run ordering vs travel-schedule

check-cfps Step 6 reads `/workspace/group/travel-schedule.json` for the travel-conflict check. That file is refreshed by the `nanoclaw-flight-assist` `nightly-travel-sync` bundle (cron `0 6 (TZ=local)`), which lands before this wrapper (cron `30 6 (TZ=local)`) when that overlay is loaded in the same group. The coupling is loose and cross-tile: check-cfps degrades gracefully when the schedule is stale or a conference's exact dates are unknown, and the wrappers gate independently, so no hard ordering dependency exists.

## When to revisit

If the standalone run still cannot reach Step 2 within a single container budget, add resumable-cycle continuation (the pattern `morning-brief` uses) rather than re-bundling. If `task_run_logs` shows weeks of `clean`, leave the cap as-is.
