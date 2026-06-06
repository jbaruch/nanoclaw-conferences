# Changelog

All notable changes to this tile are documented here.

## Unreleased

### Added

- Initial tile: `check-cfps` skill migrated from `nanoclaw-admin` into a standalone public per-chat overlay tile (`jbaruch/nanoclaw-admin#298`). The skill discovers open conference CFPs across multiple sources, applies source-aware Sessionize verification and tiered AI relevance routing, and maintains persistent per-CFP state in `cfp-state.json` with owner-side schema-version migration. Carries the eight deterministic helper scripts and their test suite unchanged from the admin original.

### Changed

- `check-cfps` description no longer claims to "extend the tessl tile version" — this tile **is** the tile version now, so the self-referential phrasing was dropped while preserving the trigger phrases.
