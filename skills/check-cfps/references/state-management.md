# cfp-state.json — State Management

Referenced from `check-cfps` SKILL.md ("State Management"). Full schema lives at `/workspace/group/cfp-state.json`; relevance criteria at `/workspace/group/RELEVANCE-CRITERIA.md`.

## Status values

`open`, `approved`, `dismissed`, `conflict`, `sent`, `remind`. User wording like "submitted to [conf]" normalizes to `status: sent`; `submitted` is not a distinct status.

## Slug format

`{conference-name-slug}-{year}` — lowercase, spaces/punctuation to hyphens.

## User feedback actions

Every row MUST also set `user_actioned: true`.

| User input | Action |
|-----------|-------|
| "sent to [conf]" / "submitted to [conf]" | `status: sent`, `user_actioned: true`, update `updated` |
| "not interested [conf]" / "skip [conf]" | `status: dismissed`, `user_actioned: true` |
| "remind me [N] days before deadline [conf]" | `status: remind`, `remind_before_days: N`, `user_actioned: true` |
| "remind me about [conf] in a week" | `status: remind`, `remind_before_days: 7`, `user_actioned: true` |
| "show again [conf]" | remove entry from cfp-state.json (clears `user_actioned`) |

## Calibration

Record Baruch's dismissal reason in `baruch_notes`. When a deadline expires with `status: "open"`, ask if he submitted and record the outcome. When Baruch restores a bot-dismissed CFP, note it in `baruch_notes` for future calibration.

## Priority interests

`matched_interests` is an orthogonal axis to `status` (jbaruch/nanoclaw-admin#308). Step 5 judges each `open`/`approved` CFP against the operator-owned priority list and records the matched interest `id`s. The morning brief partitions on this field: a non-empty list OR an absent field → pinned brief; an explicit empty list `[]` → separate non-pinned follow-up. The absent-vs-empty distinction is load-bearing — never normalise absent to `[]`.

Priorities live at `/workspace/group/cfp-priorities.json` — operator-owned, same provenance as `RELEVANCE-CRITERIA.md`, NOT shipped in the tile. The interest taxonomy is data, not code: edit this file to change what's priority (drop Java, add Rust) with no skill or schema change. Each interest carries `id`, `label`, `keywords`, `sources`, and an optional free-text `note`. Shape:

```json
{
  "priority_interests": [
    { "id": "ai",   "label": "AI",   "keywords": ["AI", "ML", "LLM", "agent", "GenAI"], "sources": [] },
    { "id": "java", "label": "Java", "keywords": ["Java", "JVM", "Kotlin", "Spring"], "sources": ["javaconferences.org"] },
    { "id": "agentcon", "label": "AgentCon (first-world only)", "keywords": ["AgentCon"], "sources": [],
      "note": "Priority only when held in a first-world country (US, Canada, EU/EEA + UK, developed Asia). Elsewhere is NOT priority." }
  ]
}
```

`keywords` and `sources` are hints for the Step 5 judgment, not literal match gates. The optional `note` is authoritative operator intent the judgment honors — use it for rules `keywords`/`sources` can't express (geo scoping, source-scoped topic filters); a `note` constraint can EXCLUDE an otherwise-matching keyword/source hit. Missing or empty file ⇒ Step 5 tags nothing AND clears `matched_interests` from the non-`user_actioned` entries it processes ⇒ the brief pins everything (no policy, no split). The clear matters: an entry that earned `[]` under a prior config would otherwise stay demoted after the config is removed.

Tagging is two-stage (jbaruch/nanoclaw-admin#308): the deterministic prefilter `scripts/match-priorities.py` consumes CFP records and emits each one's `proposed_interests`; Step 5's LLM then arbitrates — REMOVE what a `note` excludes or the description contradicts, ADD content-only matches with no proposal (e.g. "Confitura" → `java`). The prefilter never applies `note` exclusions or no-hit additions; those are judgment. The matching predicates are the script's contract — see the `match-priorities.py` top-of-file docstring.

## Schema version & ownership

Every CFP record carries its own `schema_version` (currently `1`, introduced with `matched_interests` in jbaruch/nanoclaw-admin#308) so a shape change is auditable per `coding-policy: stateful-artifacts`.

- **Owner / single writer-migrator:** `check-cfps`. Its Step 7 write phase runs `scripts/stamp-schema-version.py` — a deterministic, idempotent stamper that sets `schema_version` on EVERY record (incl. `user_actioned`, `dismissed`, `sent`), so one run reliably migrates the whole file (replacing LLM hand-stamping). `schema_version` is the one owner-metadata field exempt from the "preserve `user_actioned` entirely" rule; the user-owned decision fields stay untouched. The same run reconciles `matched_interests` (tag, clear, or preserve per the rules above).
- **Reader** (`morning-brief-cfp.py`): a non-owner reader. Per `stateful-artifacts`, a record whose `schema_version != 1` (including a legacy record with no version) is "no usable prior state" — skipped, not surfaced, until `check-cfps` migrates it. The reader never migrates; it tallies skipped records to stderr. `--mark-shown` only touches records that passed the gate and preserves their `schema_version` in place.
- **Other readers** (`dedup-by-url.py`, `system-audit.py`): operate on version-independent structural fields (`cfp_url`/slug dedup, script inventory), not the `matched_interests` shape, so this version does not gate them.
- **Migration window:** a pre-#308 file has no `schema_version` on any record; until `check-cfps` next runs (nightly housekeeping or a manual invocation) the reader surfaces no CFPs. `check-cfps`'s deterministic stamper brings every record to `1` on its next run, after which the reader resumes. No data rewrite beyond the stamp is needed — absent `matched_interests` on a version-1 record is already the pinned default.

## State format example

```json
{
  "_blocked_prefixes": ["devopsdays", "blockchain", "web3", "crypto", "gaming", "unity3d", "unreal engine", "salesforce", "sap", "sharepoint"],
  "all-things-open-2026": {
    "schema_version": 1,
    "status": "open",
    "name": "All Things Open 2026",
    "city": "Raleigh, NC, USA",
    "conf_date": "Oct 18-20",
    "deadline": "2026-03-31",
    "cfp_url": "https://allthingsopen.org/call-for-papers",
    "updated": "2026-03-31",
    "last_verified": "2026-03-31",
    "matched_interests": ["java"],
    "bot_notes": "General open-source dev conf with broad audience; typically has Java/JVM content"
  }
}
```
