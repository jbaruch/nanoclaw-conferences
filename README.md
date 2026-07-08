# jbaruch/nanoclaw-conferences

[![tessl](https://img.shields.io/endpoint?url=https%3A%2F%2Fapi.tessl.io%2Fv1%2Fbadges%2Fjbaruch%2Fnanoclaw-conferences)](https://tessl.io/registry/jbaruch/nanoclaw-conferences)

Conference CFP discovery for NanoClaw. Finds open calls-for-papers relevant to the user across Java/AI/developer conferences, ranks them by relevance, and maintains persistent per-CFP state (sent / dismissed / remind) across sessions so the same conference is never surfaced twice.

Per-chat overlay tile. Install via NanoClaw's `containerConfig.additionalTiles` mechanism.

## Capabilities

1. **Multi-source discovery** — Sessionize speaker API, `developers.events`, `javaconferences.org`, plus targeted web search for gaps
2. **Source-aware verification** — Sessionize is authority for Sessionize-sourced entries; non-Sessionize feeds are deadline-of-record; batched re-verification per run
3. **AI relevance analysis** — tiered routing (javaconferences.org auto-approve, blocklist, then AI judgement against the user's speaking topics and relevance criteria)
4. **Persistent state** — `sent` / `dismissed` / `remind` / `approved` / `conflict` status per CFP in `cfp-state.json`, immutable once the user acts on an entry
5. **Travel-conflict detection** — cross-checks CFP conference dates against the trip schedule and flags overlaps
6. **Priority-interest tagging** — deterministic prefilter plus AI arbitration against an owner-defined priority list

## Installation

```
tessl install jbaruch/nanoclaw-conferences
```

Add to a chat's overlay tile list via `update_group_config`:

```
additionalTiles: ["nanoclaw-conferences"]
```

Load the overlay at the **main or trusted** tier — the skill reads `/workspace/trusted/user_professional.md` for the user's speaking topics.

## Required environment

The CFP pipeline calls the Sessionize universal API deterministically from its own scripts (so the large responses never enter the agent's context — see jbaruch/nanoclaw-conferences#7/#9), which needs two host-injected keys in the container environment:

| Variable | Used by | Sent as |
|----------|---------|---------|
| `SESSIONIZE_SPEAKER_KEY` | `discover-open-cfps.py` (Step 2) | `X-API-KEY` header to `.../open-cfps` |
| `SESSIONIZE_EVENT_API_KEY` | `verify-sessionize.py` (Step 5) | `X-API-KEY` header to `.../event?slug=` |

`SESSIONIZE_API_BASE` optionally overrides the `https://sessionize.com/api/universal` base (tests / proxying). These are the same keys the NanoClaw host already holds for its `sessionize_*` tools; the tile reads them from the injected environment and bundles no secret of its own. Remaining data comes from the host `fetch_markdown` tool and public JSON feeds (`developers.events`, `javaconferences.org`); the `sessionize_*` MCP tools remain available for ad-hoc queries.

## Runtime data

The skill reads and writes files under the shared `/workspace/group/` mount:

| File | Access | Owner |
|------|--------|-------|
| `cfp-state.json` | read+write | this tile (schema-versioned, owner-migrated) |
| `cfp-suppressed-today.json` | write | this tile |
| `cfp-priorities.json` | read | owner-managed |
| `RELEVANCE-CRITERIA.md` | read | owner-managed |
| `travel-schedule.json` | read | written by `nanoclaw-admin`'s `nightly-external-sync` (co-loaded) |
| `user_professional.md` | read (`/workspace/trusted/`) | owner-managed |

Reads of admin-owned files resolve because admin co-loads with this overlay in the same chat via the shared mount; each is optional and degrades gracefully when absent.

## Skills

| Skill | Description |
|-------|-------------|
| [check-cfps](skills/check-cfps/SKILL.md) | Finds open CFPs relevant to the user across Java/AI/developer conferences and maintains persistent CFP state (sent/dismissed/remind) in `cfp-state.json`. Use when the user asks about upcoming conferences, call for papers, speaking opportunities, CFP deadlines, or where to submit a talk proposal. |
| [nightly-cfp-sync](skills/nightly-cfp-sync/SKILL.md) | Cadence wrapper (cron `30 6`, precheck-gated by a filesystem cadence cursor) that runs `check-cfps` on a schedule, consumes the CFP list internally, and surfaces only a stale-verification notice. Emits the observable-silence cursor marker the silent-success watchdog reads. |

## Skill scripts

The skill bundle includes deterministic scripts the agent invokes from the SKILL.md steps:

- `scripts/check-cfps-fetch.py` — fetches + filters CFPs from the public feeds, applies the source-list and blocklist
- `scripts/discover-open-cfps.py` — Step 2 deterministic Sessionize open-CFP discovery via the speaker API (candidate shape and filter rules in its docstring)
- `scripts/prepare-sessionize-batch.py` — routes entries by effective source and derives Sessionize slugs for a single batched verification call
- `scripts/apply-sessionize-results.py` — joins batched Sessionize results back to entries and emits per-entry verdicts
- `scripts/verify-sessionize.py` — Step 5 verification driver: collapses prepare → live universal-API round-trip → apply into one invocation and writes the `verify-evidence.json` marker
- `scripts/backfill-source.py` — infers a missing `source` from the `cfp_url` host
- `scripts/backfill-name.py` — derives a fallback `name` for nameless records from the slug or `cfp_url`
- `scripts/expire-cfps.py` — expires stale non-Sessionize `open`/`approved` entries whose deadline passed
- `scripts/match-priorities.py` — deterministic priority-interest prefilter
- `scripts/dedup-by-url.py` — collapses entries whose `cfp_url` normalises to the same host+path, preserving the highest-priority source attribution and inheriting missing metadata from dropped copies
- `scripts/commit-state.py` — lock-owning committer for the Step 8 working-set write (the agent never writes `cfp-state.json` directly)
- `scripts/stamp-schema-version.py` — owner-side `schema_version` stamper for `cfp-state.json`
- `scripts/stamp-last-checked.py` — evidence-gated single writer of the `_last_checked` freshness heartbeat (exit 3 = verification not evidenced)
- `scripts/run-state.py` — per-run checkpoint store for resumable runs (`begin`/`save`/`load`/`invalidate`/`done`; schema in `references/run-state.md`)
- `scripts/audit-sessionize-key-drift.py` — reports Sessionize slug drift in stored state
- `scripts/state_lock.py` — shared advisory-flock module (not a CLI) that serialises every `cfp-state.json` mutator's read-modify-write

The `nightly-cfp-sync` cadence wrapper carries its own scripts:

- `scripts/precheck-nightly-cfp-sync.py` — fire-time precheck that gates wake-ups by the cadence cursor
- `scripts/stamp-cursor.py` — advances the `nightly-cfp-sync-cursor.json` success cursor after a clean run

## Status

- **V1.1** — adds the `nightly-cfp-sync` cadence wrapper alongside `check-cfps`, so the tile owns both the user-driven CFP lookup and its scheduled refresh (mirrors `nanoclaw-flight-assist` bundling its `sync-tripit` cadence driver with `check-travel-bookings`). The wrapper materialises one `scheduled_tasks` row in chats that load this overlay.
- **V1** — migrated `check-cfps` from `nanoclaw-admin` as a standalone per-chat overlay tile. Full multi-source discovery, source-aware Sessionize verification, AI relevance routing, persistent state with owner-side schema migration, travel-conflict detection, and priority-interest tagging.

See [CHANGELOG.md](CHANGELOG.md) for version history.
