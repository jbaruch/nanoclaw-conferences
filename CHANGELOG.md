# Changelog

All notable changes to this tile are documented here.

## Unreleased

### Added

- `nightly-cfp-sync` cadence wrapper migrated from `nanoclaw-admin` (`jbaruch/nanoclaw-admin#298`). It runs `check-cfps` on a 3-day-capped `30 6` cadence, consumes the CFP list internally, surfaces only a stale-verification notice, and emits the observable-silence cursor marker. Co-locating the cadence driver with the skill it drives keeps the CFP domain self-contained in one tile (same pattern as `nanoclaw-flight-assist`'s `sync-tripit` + `check-travel-bookings`) and removes the cross-tile `Skill()` call that would otherwise span admin → conferences. Carries its precheck + stamp-cursor scripts, `cadence-rationale.md` / `state-schema.md` references, and both unit tests unchanged from the admin original.

## 0.1.0

### Added

- Initial tile: `check-cfps` skill migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#298`). The skill discovers open conference CFPs across multiple sources, applies source-aware Sessionize verification and tiered AI relevance routing, and maintains persistent per-CFP state in `cfp-state.json` with owner-side schema-version migration. Carries the eight deterministic helper scripts and their test suite unchanged from the admin original.

### Changed

- `check-cfps` description no longer claims to "extend the tessl tile version" — this tile **is** the tile version now, so the self-referential phrasing was dropped while preserving the trigger phrases.
