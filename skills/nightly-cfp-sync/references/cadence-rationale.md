# Cadence rationale

The check-cfps run is heavy — full-cohort Sessionize verification across every stored `open`/`approved` entry, plus web-search gap discovery — so this wrapper runs it in its own bounded container on a capped cadence rather than inline with other maintenance.

## Why a cadence cap

CFP deadlines move on a multi-day horizon, so refreshing `cfp-state.json` periodically rather than daily keeps it current without paying the verification cost every day. The precheck enforces this with a filesystem cursor at `/workspace/group/state/nightly-cfp-sync-cursor.json`. The cap value and the wake/skip predicate are the script's contract — see `scripts/precheck-nightly-cfp-sync.py` (`CADENCE` constant + the cursor-read gate).

The cap sits below the multiple of the daily cron interval that would name the intended cadence, not at it: the cursor stamps at run completion, so a cap at an exact multiple leaves the intended fire a few minutes short and slips the run by a whole day (`jbaruch/nanoclaw#803` / `nanoclaw-admin#353`). The near-miss rationale lives in the precheck's `CADENCE` comment.

## Run ordering vs travel-schedule

check-cfps Step 7 reads `/workspace/group/travel-schedule.json` for its travel-conflict check. When the `nanoclaw-flight-assist` overlay is co-loaded, its travel-sync wrapper refreshes that file on an earlier cron slot, so this wrapper usually sees fresh data. The coupling is loose: check-cfps degrades gracefully when the schedule is stale or a conference's exact dates are unknown, and the wrappers gate independently — no hard ordering dependency exists.
