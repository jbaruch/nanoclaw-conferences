# Changelog

All notable changes to this plugin are documented here.

## 0.1.22 — 2026-07-10

### Changed

- `check-cfps` SKILL.md no longer says the `mcp__nanoclaw__sessionize_open_cfps` / `sessionize_get_events` tools "stay available for ad-hoc queries" — those host IPC handlers and their MCP shims were retired in `jbaruch/nanoclaw#769` (they were ad-hoc/overlay concerns that had leaked into core `ipc.ts`). The deterministic path is unchanged: the `discover-open-cfps.py` / `verify-sessionize.py` drivers own the Sessionize round-trip in-container, now reaching the API directly with the placeholder `X-API-KEY` the OneCLI gateway swaps. Ad-hoc Sessionize queries are an in-container API call, not an MCP tool. (Follow-up: the two driver docstrings still carry historical "mirrors the host's `sessionize_*` handler" references — maintainer-facing only, to be scrubbed separately.)

## 0.1.21 — 2026-07-08

### Changed

- Migrated off the deprecated `tile.json` / `.tileignore` packaging (jbaruch/nanoclaw-conferences#37): the manifest now lives at `.tessl-plugin/plugin.json` (via `tessl plugin migrate`), `.tileignore` became `.tesslignore`, and the obsolete `tile.json` is gone. The publish workflow was renamed `publish-tile.yml` → `publish-plugin.yml` ("Review & Publish Plugin") and now runs `tessl plugin lint`. Package-sense "tile" wording across scripts, docs, and ignore-file comments reads "plugin"; the NanoClaw host concept (`containerConfig.additionalTiles`, "overlay tile") and historical changelog entries keep their original wording.

## 0.1.20 — 2026-07-08

### Fixed

- Release/docs metadata caught up with shipped state (jbaruch/nanoclaw-conferences#34): backfilled the missing `0.1.15` changelog entry and refreshed the README script inventory with the runtime scripts the workflow already invokes (`discover-open-cfps.py`, `verify-sessionize.py`, `run-state.py`, `stamp-last-checked.py`, `state_lock.py`, `commit-state.py`).

## 0.1.19 — 2026-07-08

### Fixed

- cfp-state.json writers can no longer lose each other's updates (jbaruch/nanoclaw-conferences#35). The read-modify-write in every mutator (`backfill-source.py`, `backfill-name.py`, `dedup-by-url.py`, `expire-cfps.py`, `stamp-schema-version.py`, `stamp-last-checked.py`) now runs under a shared advisory `fcntl.flock` on a sibling `cfp-state.json.lock` file — new `state_lock.py` module, loaded via the existing sibling-import pattern. The previous temp-file + `os.replace` discipline only prevented partial files; two concurrent writers reading the same old state still silently dropped whichever write landed first. Lock acquisition blocks up to 30s (CFP_STATE_LOCK_TIMEOUT env override), then exits 1 with a diagnostic. Read-only consumers (`check-cfps-fetch.py`) stay lock-free — `os.replace` already guarantees them a complete snapshot. A threaded regression test proves a concurrently committed record survives another writer's RMW.
- The Step 8 state write itself moved behind the same lock: new `scripts/commit-state.py` is the single committer for the run's working set (the agent never writes cfp-state.json directly). It applies per-slug replacements against a fresh locked read, so concurrent updates to other slugs survive, and re-checks `user_actioned: true` on disk at commit time so a mid-run user action is never overwritten. The external `morning-brief --mark-shown` writer is tracked separately for lock adoption.

## 0.1.18 — 2026-07-07

### Fixed

- Failed verification checkpoints are now invalidated before a same-day retry (jbaruch/nanoclaw-conferences#31). When `stamp-last-checked.py` exits 3 (verification not evidenced), the run-state store used to keep the saved `verify`/`working_set` artifacts, so a same-day retry resumed straight to Step 8 and repeated the heartbeat refusal without a new Sessionize call. New `run-state.py invalidate <stage>...` subcommand removes named stage artifacts (plus the driver's `verify-evidence.json` marker) and drops them from `manifest.completed`; SKILL.md Step 8 item 11 now runs it on exit 3 so the retry keeps `fetch`/`candidates` but re-runs Step 5 live (renumbered to item 12 by 0.1.19's commit-state step).

## 0.1.17 — 2026-07-07

### Fixed

- developers.events epoch-millisecond fields (`untilDate`, `conf.date`) now convert to dates in explicit UTC (jbaruch/nanoclaw-conferences#36). The previous naive `date.fromtimestamp` used the host timezone, shifting deadlines and conference dates by a day on non-UTC runners — affecting `days_left`, remind windows, sorting, travel-conflict checks, and expiry. The test fixture that masked this (a UTC-forcing `fromtimestamp` monkeypatch) is gone; a regression test now runs the real conversion under a UTC+14 host zone.
- `make_slug` no longer keys yearless conference names under the current calendar year (jbaruch/nanoclaw-conferences#33). Fallback year priority is now: year embedded in the name → `conf_date` year → `deadline` year → current year, so a recurring conference whose feed name omits the year keys under its own edition's year and keeps matching its cfp-state row across year boundaries.

## 0.1.16 — 2026-07-07

### Fixed

- `check-cfps-fetch.py` no longer fails open when `cfp-state.json` exists but cannot be read or parsed (jbaruch/nanoclaw-conferences#30). Previously any read/parse failure was downgraded to a warning and an empty state, silently dropping `sent`/`dismissed`/`remind`/`_blocked_prefixes` filtering so already-actioned CFPs re-entered the candidate pool as new. Now the script exits 1 with a stderr diagnostic and produces no output; SKILL.md Step 3 already aborts on non-zero exit. An absent state file (first run) still means empty state.
- State-maintenance scripts now return their documented exit-1 stderr diagnostic when `cfp-state.json` exists but is not valid UTF-8, instead of escaping with an unhandled `UnicodeDecodeError` traceback (jbaruch/nanoclaw-conferences#32): `audit-sessionize-key-drift.py`, `backfill-name.py`, `backfill-source.py`, `dedup-by-url.py`, `expire-cfps.py`. `stamp-schema-version.py` likewise now reports a write-side `OSError` (e.g. read-only state directory) as a diagnostic + exit 1 instead of a traceback.

## 0.1.15 — 2026-07-07

### Changed

- Added `.github/mcp.json` (GitHub Copilot CLI MCP config emitted by current tessl CLI init) to the tessl consumer-side scaffolding ignore block, which predated it (jbaruch/nanoclaw-conferences#29). No skill or script changes. (Entry backfilled by #34 — the release shipped without one.)

## 0.1.14 — 2026-07-07

### Fixed

- Non-Sessionize `open`/`approved` CFP entries now expire when their deadline passes (jbaruch/nanoclaw-conferences#27). Previously nothing ever re-checked a stored deadline-of-record row against its own deadline, so closed CFPs lingered as `open` forever — and each run refreshed their `last_verified`, so they never aged out either (33 zombie rows found in live state on 2026-07-07). New `scripts/expire-cfps.py` — the single writer of the new `expired` status — runs in Step 5 pre-verify: past-deadline non-Sessionize rows become `expired` (with a `bot_notes` marker) and drop out of the verify cohort. `user_actioned` rows are never touched; Sessionize rows (explicit or host-inferred) are left to the live-verify "MISSED" path; a CFP re-listed upstream with an extended deadline revives automatically since the fetcher's state filter does not hide `expired` slugs.

## 0.1.13 — 2026-07-07

### Fixed

- `check-cfps` dedup no longer silently drops the javaconferences.org attribution (and the `name`) when a conference appears in multiple feeds with a self-hosted CFP URL (jbaruch/nanoclaw-conferences#25, Devoxx Morocco). `dedup-by-url.py` gains a source-priority winner tier (javaconferences.org > sessionize-speaker-api > developers.events) between the source-host-match rule and the alphabetical tiebreak, and the merge now fills the winner's missing `name`/`city`/`conf_date` from the dropped copies (never `deadline`; `user_actioned` winners stay untouched). New `fields_inherited` counter in the script's output.
- Duplicate slugs can no longer resurrect after a dedup merge (jbaruch/nanoclaw-conferences#24, vibe-coding-con). The Step 8 `--lookup` key-rewrite now covers the full in-memory working set (stored rows included, not just Steps 2–4 candidates), and a post-write dedup re-run guards the on-disk state so no run ends with two slugs for one CFP.
- Nameless CFP records no longer rot invisibly (jbaruch/nanoclaw-conferences#23, Devoxx Morocco expired unsurfaced). New `scripts/backfill-name.py` derives a fallback display name from the slug (year suffix stripped, title-cased) or the `cfp_url`, runs at the top of Step 5 so every stored record is visible to `match-priorities.py` and Tier-3 relevance analysis, and doubles as the one-shot repair for already-damaged state. `user_actioned: true` entries are never touched (immutability invariant); nameless ones surface in a `skipped_user_actioned` counter.

## 0.1.9 — 2026-07-06

### Added

- `nightly-cfp-sync` declares a host-checked work-evidence contract: `evidence: "cfp-state.json#_last_checked"` frontmatter (jbaruch/nanoclaw#720/#721). After every fire, the host scheduler deterministically verifies `_last_checked` was advanced during the run; a run that reports success without freshening the cursor is recorded as `evidence-check:` error and its pinned SDK session is cleared so the next fire starts fresh. Complements the in-tile evidence gating (#8), which a session-precedent-polluted agent was observed to bypass by fabricating the stamper's report format wholesale — the host-side check cannot be fabricated from inside the container.

## 0.1.8 — 2026-07-01

### Changed

- Bumped the pinned `ruff` from `0.7.4` to `0.15.20` and reformatted the tree to match. Pure style (0.15.20 collapses adjacent implicit string concatenations that now fit on one line); no behavior change. Doing the bump-plus-reformat together keeps a bare Dependabot version-bump from landing red against the `ruff format --check` gate.

## 0.1.7 — 2026-06-30

### Changed

- The `check-cfps` Sessionize round-trips moved out of the agent's context into deterministic scripts that call the Sessionize universal API directly, closing the "skip to save tokens" hole behind issues `jbaruch/nanoclaw-conferences#4`/#7/#8/#9. The MCP `sessionize_*` tools stay available for ad-hoc queries; only the pipeline path changed. Because a Python subprocess can't invoke an MCP tool, the ~260 KB open-CFP and ~267 KB events payloads used to land in the agent's own context, where a token-pressured Haiku run would improvise inline Python — misparsing the open-CFP list as "1 event" (0 new candidates on 2026-06-29) and skipping the live verify call while faking verdicts from memory yet recording success.
  - **Verification driver (#7):** new `scripts/verify-sessionize.py` collapses Step 5's prepare → events round-trip → apply into one invocation. It fetches each slug from `https://sessionize.com/api/universal/event?slug=` (`X-API-KEY: SESSIONIZE_EVENT_API_KEY`), normalizing each raw event in-tile via a direct port of the host's `normalizeSessionizeEvent`. Per-slug failures isolate to `verify_failed` (bounded concurrency, the host's batch contract) and never substitute remembered verdicts.
  - **Discovery (#9):** new `scripts/discover-open-cfps.py` calls `.../open-cfps` (`X-API-KEY: SESSIONIZE_SPEAKER_KEY`), applies the host's online/user-group filter, and emits the Step 2 candidate count deterministically instead of parsing the payload inline.
  - **Evidence-gated freshness (#8):** `scripts/stamp-last-checked.py` no longer advances `_last_checked` unconditionally. The driver writes a `verify-evidence.json` marker, and the stamp advances the heartbeat only when ≥1 entry was resolved from a live response this run (or there was nothing to verify). A skipped call or total Sessionize outage now records `_last_checked_skipped` and exits 3 — so the run can't report a clean success or fool the `jbaruch/nanoclaw#601` work-evidence watchdog. `nightly-cfp-sync` reads the new `verification` field and, on `"skipped"`, holds the cadence cursor and retries next fire instead of resting 72h on an unverified run.
  - The run-state checkpoint store's `prep`/`sessionize_results`/`decisions` stages collapse into a single `verify` stage.

### Required environment

- The CFP pipeline now reads two host-injected Sessionize keys from the container environment: `SESSIONIZE_SPEAKER_KEY` (open-CFP discovery) and `SESSIONIZE_EVENT_API_KEY` (event verification) — the same keys the NanoClaw host already held for its `sessionize_*` tools. `SESSIONIZE_API_BASE` optionally overrides the API base. See `.env.example` and README "Required environment."

## 0.1.6 — 2026-06-30

### Added

- Pyright is now gated in CI at zero findings (`jbaruch/nanoclaw-conferences#6`, adopting `jbaruch/coding-policy: language-diagnostics`): new `pyrightconfig.json` (basic, py3.11, over `skills` + `tests`), a pinned `pyright==1.1.390`, and a `python -m pyright` step between ruff and pytest. Turning the gate on for a never-checked tree is landed as its own focused change, separate from feature work, per the rule.
- `.github/dependabot.yml` gives the pinned dev dependencies (`pytest`, `ruff`, `pyright`) and the GitHub Actions a stated, automated renewal mechanism, per `jbaruch/coding-policy: dependency-management`.

### Fixed

- The first pyright run surfaced 13 real findings, all fixed with typed None-guards (no blanket suppressions): unchecked `importlib` `spec_from_file_location` (`ModuleSpec | None`) access in the `prepare-sessionize-batch.py` sibling loader, the `conftest.py` loader, and three test-module loaders; and `__doc__`-is-`None` (under `-OO`) before `.splitlines()[0]` in `stamp-cursor.py`.

## 0.1.5

### Added

- `check-cfps` now stamps an honest freshness heartbeat and resumes interrupted runs from disk (`jbaruch/nanoclaw-conferences#4`).
  - **Freshness:** new deterministic `scripts/stamp-last-checked.py` is the single writer of `cfp-state.json`'s top-level `_last_checked`, run in Step 8 on every successful pass. It stamps the run timestamp unconditionally (a run that re-verified everything and changed nothing still "checked"). Replaces LLM hand-writing, which had left the field frozen at `2026-04-22` while every record had refreshed days earlier — on 2026-06-13 that stale field drove a wrong "pipeline stalled ~7 weeks" live diagnosis when the pipeline was in fact healthy (running on its ~72h cadence). `_last_checked` is distinct from the wrapper's `nightly-cfp-sync-cursor.json` `last_run` (which gates cadence); both exist on purpose.
  - **Resumability:** new `scripts/run-state.py` checkpoint store persists each pipeline stage artifact (`fetch`, `candidates`, `prep`, `sessionize_results`, `decisions`, `working_set`) under `/workspace/group/state/cfp-run/`, with a Step 1 resume guard and clear-on-success teardown in the SKILL. Motivated by the 2026-06-10 run, a token-limit continuation that had hand-rolled the whole pipeline inline, blew its budget mid-run, and — finding its prep output was never persisted — re-derived it and re-discovered the `cfp-state.json` schema from a chat summary. The store is per-UTC-day (a continuation on a later day resets to a fresh run); resume is best-effort since Step 5 re-verifies the full cohort, so a fresh full run is always safe. It is a flat directory of JSON files rather than a `messages.db` table — a short-lived, single-installation, single-writer scratch artifact with no cross-row queries, torn down on success — same basis as the `nightly-cfp-sync` cursor.

## 0.1.4

### Changed

- Pinned `nightly-cfp-sync` to Haiku (`claude-haiku-4-5-20251001`) via `agentModel:` frontmatter — CFP data re-verify/sync is triage, not synthesis, so it no longer defaults to Opus. Part of the #613 Claude tier-down (`jbaruch/nanoclaw#613`).

## 0.1.3

### Changed

- Reworded `skills/check-cfps/references/web-fetch-fallback.md` to describe `fetch_markdown` as the NanoClaw host's server-side renderer in neutral terms, dropping the `snitchmd` / `CloakBrowser` / "past anti-bot gates" wording the registry's intent-review moderation repeatedly misread as an attacker-proxy prompt-injection (false positive — these are first-party NanoClaw rendering services). Behavior is unchanged: same `fetch_markdown`-then-Composio fallback chain. Also dropped the cross-tile reference to the admin-only `max-effort` skill so the public tile is self-contained. Ends the per-publish moderation-override treadmill for this tile.

## 0.1.1

### Added

- `nightly-cfp-sync` cadence wrapper migrated from `nanoclaw-admin` (`jbaruch/nanoclaw-admin#298`). It runs `check-cfps` on a 3-day-capped `30 6` cadence, consumes the CFP list internally, surfaces only a stale-verification notice, and emits the observable-silence cursor marker. Co-locating the cadence driver with the skill it drives keeps the CFP domain self-contained in one tile (same pattern as `nanoclaw-flight-assist`'s `sync-tripit` + `check-travel-bookings`) and removes the cross-tile `Skill()` call that would otherwise span admin → conferences. Carries its precheck + stamp-cursor scripts, `cadence-rationale.md` / `state-schema.md` references, and both unit tests unchanged from the admin original.
  - Origin: `nightly-cfp-sync` was peeled off the `nightly-external-sync` bundle in `jbaruch/nanoclaw#581` so the heavy full-cohort CFP verification gets its own bounded container instead of being cut off in the bundle's long tail — which had left `task_run_logs.result` empty. (Moved here from the reference doc per `coding-policy: context-writing-style` — incidents live in the CHANGELOG, not auto-loaded context.)

### Rules

- **Closed-loop carve-out claimed for `jbaruch/coding-policy: plugin-evals`** (2026-06-06). This tile is part of the `jbaruch/nanoclaw-*` plugin fleet — a fully-automated agent loop satisfying all three preconditions of the rule's "Narrow exception for closed-loop automated systems with no human eval-result consumption" clause: (1) no human reviews eval output for this tile in any form (no eval scores, no lift deltas, no scenario-by-scenario diffs, no regression alerts); (2) no automated gate consumes eval results (no `evals.yml` workflow, no publish-tile eval step, no downstream dashboard or paging route); (3) the owner accepts that re-introducing any consumption of eval results later — whether human review OR automated gating — requires re-introducing evals first under the standard requirement. Matches the carve-out claimed by `jbaruch/nanoclaw-admin` on 2026-05-09 and inherited by every `jbaruch/nanoclaw-*` tile thereafter (e.g. `jbaruch/nanoclaw-flight-assist`, 2026-05-18). Covers both decisional skills in this tile (`check-cfps`, `nightly-cfp-sync`). No `evals/` directory ships in this tile.

## 0.1.0

### Added

- Initial tile: `check-cfps` skill migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#298`). The skill discovers open conference CFPs across multiple sources, applies source-aware Sessionize verification and tiered AI relevance routing, and maintains persistent per-CFP state in `cfp-state.json` with owner-side schema-version migration. Carries the eight deterministic helper scripts and their test suite unchanged from the admin original.

### Changed

- `check-cfps` description no longer claims to "extend the tessl tile version" — this tile **is** the tile version now, so the self-referential phrasing was dropped while preserving the trigger phrases.
