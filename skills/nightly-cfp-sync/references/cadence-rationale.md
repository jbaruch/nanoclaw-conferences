# Cadence rationale

`nightly-cfp-sync` exists because the check-cfps run is heavy — full-cohort Sessionize verification across every stored `open`/`approved` entry, plus web-search gap discovery. It was peeled off the `nightly-external-sync` bundle (`jbaruch/nanoclaw#581`) so that long tail gets its own bounded container instead of exhausting the bundle before its final summary and leaving `task_run_logs.result` empty.

## Why a cadence cap

CFP deadlines move on a multi-day horizon, so refreshing `cfp-state.json` periodically rather than daily keeps it current without paying the verification cost every day. The precheck enforces this with a filesystem cursor at `/workspace/group/state/nightly-cfp-sync-cursor.json`. The cap value and the wake/skip predicate are the script's contract — see `scripts/precheck-nightly-cfp-sync.py` (`CADENCE` constant + the cursor-read gate).

## Run ordering vs travel-schedule

check-cfps Step 6 reads `/workspace/group/travel-schedule.json` for its travel-conflict check. When the `nanoclaw-flight-assist` overlay is co-loaded, its travel-sync wrapper refreshes that file on an earlier cron slot, so this wrapper usually sees fresh data. The coupling is loose: check-cfps degrades gracefully when the schedule is stale or a conference's exact dates are unknown, and the wrappers gate independently — no hard ordering dependency exists.
