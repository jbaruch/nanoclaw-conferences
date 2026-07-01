# Changelog

All notable changes to this tile are documented here.

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
