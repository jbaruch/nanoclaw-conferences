# Changelog

All notable changes to this tile are documented here.

## Unreleased

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
