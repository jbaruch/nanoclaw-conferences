# Run-state checkpoint store — `cfp-run/`

Referenced from `check-cfps` SKILL.md (Resume guard). Per `coding-policy: stateful-artifacts`, every stateful artifact ships a schema document next to its owner skill; this file is that document for the run-state checkpoint store — the per-stage scratch artifacts check-cfps writes so an interrupted run resumes from disk.

## Path

`/workspace/group/state/cfp-run/` (overrideable per-process via the `CFP_RUN_STATE_DIR` env var, used by tests). A flat directory:

```
cfp-run/
  manifest.json        run bookkeeping
  fetch.json           saved stage artifacts (one file per saved stage)
  candidates.json
  verify.json
  working_set.json
  verify-evidence.json the Step 5 driver's verification marker (read by the Step 8 stamp gate)
```

## Owner

`tessl__check-cfps` (this skill). Written and cleared exclusively by `scripts/run-state.py`. The wrapper `nightly-cfp-sync` never touches it — it calls `check-cfps`, which manages its own run-state internally.

## Reader

`scripts/run-state.py load <stage>` only. No other skill or script reads these artifacts; they are scratch state for a single in-flight run, distinct from `cfp-state.json` (the durable, owned CFP data).

## manifest.json shape (schema_version 1)

```json
{
  "schema_version": 1,
  "run_date": "2026-06-13",
  "completed": ["fetch", "candidates", "verify"]
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | integer | yes | Currently `1`. Bump on shape change. `begin` resumes only when this equals the supported version; a mismatch (future/partial shape) is treated as no usable prior run and resets. |
| `run_date` | string | yes | UTC date (`YYYY-MM-DD`) the run began. `begin` resumes only when this equals today; otherwise it resets. |
| `completed` | string[] | yes | Stage names saved so far, in completion order, deduped. |

## Stages

The check-cfps pipeline checkpoints these stages in order. Each is the JSON artifact a step already produces — save it as soon as the step yields it, so the next continuation resumes from the first stage NOT in `completed`.

| Stage | Produced after | Artifact |
|-------|----------------|----------|
| `fetch` | Step 3 fetch script | `check-cfps-fetch.py` stdout (`{cfps, warnings, checked_at}`) |
| `candidates` | Steps 2–4 merge | the merged, slug-deduped candidate pool (Sessionize + fetch + web-search) |
| `verify` | Step 5 driver | `verify-sessionize.py` stdout (`{prep, results, decisions, summary, non_sessionize, evidence}`) |
| `working_set` | Steps 5–7 | the in-memory entry set (verified + relevance + travel applied) about to be written in Step 8 |

Stage names are free-form lowercase identifiers (`[a-z0-9][a-z0-9_-]*`); the table above is the check-cfps contract, not a hard-coded enum in the script.

## Commands

```bash
# Start or resume. Resets across a UTC-day boundary; resumes within the same day.
python3 .../run-state.py begin
# -> {"resume": false, "run_date": "2026-06-13", "completed": []}
# -> {"resume": true,  "run_date": "2026-06-13", "completed": ["fetch", "verify"]}

# Persist a stage artifact (JSON on stdin).
echo '<artifact json>' | python3 .../run-state.py save verify
# -> {"saved": "verify"}

# Reload a saved stage on resume.
python3 .../run-state.py load verify          # prints the artifact; exit 2 if never saved

# Clear on success (end of Step 8, after the state write + stampers).
python3 .../run-state.py done                 # -> {"cleared": true}

# Drop failed stages (and the driver's evidence marker) so a same-day
# retry re-runs them instead of reloading failed output. Idempotent.
python3 .../run-state.py invalidate verify working_set verify-evidence
# -> {"invalidated": ["verify", "working_set"], "absent": ["verify-evidence"]}
```

## Lifecycle

- **Fresh run** — `begin` finds no usable manifest (absent, unreadable, stale `run_date`, or unsupported `schema_version`), clears any leftover files, writes a fresh manifest, returns `resume: false`. The agent runs Steps 2–8, calling `save <stage>` as each artifact appears.
- **Resumed run** — a token-limit continuation re-invokes the skill; `begin` finds today's manifest and returns `resume: true` with `completed`. The agent `load`s each completed stage instead of recomputing it and resumes at the first uncompleted stage.
- **Success** — Step 8 finishes the state write and stampers, then calls `done` to remove the directory. The next run starts clean.
- **Failure** — on a technical failure the agent stops without `done`; artifacts persist so a same-day retry resumes. A retry on a later UTC day resets to a fresh full run.
- **Verification-gate failure** — when `stamp-last-checked.py` exits 3 (verification not evidenced), the agent keeps the store but runs `invalidate verify working_set verify-evidence` first (SKILL.md Step 8 item 11). Without this, a same-day retry would resume from the saved `verify`/`working_set` artifacts and repeat the heartbeat refusal without a new Sessionize call; with it, the retry reloads `fetch`/`candidates` and re-runs Step 5 live.

Resume is best-effort: a fresh full run is always safe (it does not depend on any saved artifact), so a missing or reset store only costs redone work, never correctness.
