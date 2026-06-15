---
name: check-cfps
description: Finds open CFPs relevant to Baruch across Java/AI/developer conferences and maintains persistent CFP state (sent/dismissed/remind) in cfp-state.json. Use when Baruch asks about upcoming conferences, call for papers, speaking opportunities, CFP deadlines, or where to submit a talk proposal.
---

# Check CFPs (with State Management)

Process steps in order. Do not skip ahead.

Fetches open CFPs from multiple sources via `scripts/check-cfps-fetch.py`, applies routing + AI-based relevance analysis in Step 6, and maintains persistent state across sessions. The fetcher owns source-list and blocklist filtering; tier-based routing (including the javaconferences.org auto-approve path) is the agent's work in Step 6.

## Contracts

The skill's write invariants (dedup-artifact ban, immutable `user_actioned`, dismissal-reason discipline, `last_verified` surfacing gate, no-silent-defer, budget-low-is-not-a-defer-reason) and the Step 5 verification-failure protocol (`_verify_failed`, `⚠️ STALE DATA` prefix, caller-visible counts) live in `references/contracts.md`. Read once, apply throughout.

## Step 1 — Resume guard

Run this first, before any other step. This pipeline can be interrupted mid-run by a token-limit continuation. To resume from disk instead of reconstructing the working set from chat history, open (or start) the run's checkpoint store:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py begin
```

- `{"resume": false}` — fresh run. Proceed from Step 2.
- `{"resume": true, "completed": [...]}` — a run begun earlier today was interrupted. For each stage already in `completed`, reload its artifact with `run-state.py load <stage>` instead of recomputing it, and resume at the first step whose stage is absent.

Stages, in pipeline order: `fetch` (Step 3), `candidates` (Steps 2–4 merge), `prep` / `sessionize_results` / `decisions` (Step 5), `working_set` (Steps 5–7, ready for Step 8). After producing each stage's artifact, persist it:

```bash
echo '<artifact json>' | python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py save <stage>
```

Resume is best-effort — stages are idempotent and Step 5 re-verifies the full cohort, so a fresh run is always safe; the store only avoids redoing expensive work. It is per-UTC-day (a continuation on a later day resets). Stage shapes, lifecycle, and the day-boundary reset: `references/run-state.md`.

## Step 2 — Sessionize speaker API candidates

```
mcp__nanoclaw__sessionize_open_cfps(filter: {isOnline: false, isUserGroup: false})
```

For each event, extract the slug from `cfpLink` (last path segment). Skip slugs already in `/workspace/group/cfp-state.json` (any status). Otherwise add to the candidate pool: `name`, `city`, `conf_date`, `deadline` (from `cfpDates.endUtc[:10]`), `cfp_url`, `slug`, `source: "sessionize-speaker-api"`. Do not write to state here.

## Step 3 — Run fetch-and-filter script

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/check-cfps-fetch.py
```

Parse JSON output: `cfps`, `warnings`, `checked_at`. **Checkpoint:** `save fetch` (the script's stdout) before merging. Then merge Sessionize candidates from Step 2, dedup by slug. Tier-1 auto-approve is NOT guaranteed on name collisions; where you must choose between equivalent rows, keep the one with more complete metadata. Surface `warnings` at the top of output. Abort if script fails.

## Step 4 — Web search for gaps

Read `/workspace/trusted/user_professional.md` for Baruch's current speaking topics. Construct 2–3 web search queries from his actual topics combined with CFP discovery terms. Add new CFPs not already in the list (dedup by conference name). Apply hard filters (no online/virtual, no excluded locations). Do not apply relevance filtering yet.

**Checkpoint:** once the full candidate pool is assembled (Steps 2–4 merged and deduped), `save candidates` (the merged pool) before Step 5.

**JS-rendered CFP pages.** Plain `WebFetch` often returns empty SPA shells; use the `fetch_markdown` → Cloudflare-Browser-Rendering fallback chain — see `references/web-fetch-fallback.md` (same chain applies in Steps 6 and 7).

## Step 5 — Source-aware verification

Verify two cohorts:
- **New candidates** from Steps 2–4.
- **Already-stored `open`/`approved` entries** — every slug in `cfp-state.json` with `status in (open, approved)`.

**Routing is source-aware** — Sessionize is authority only for `source == "sessionize-speaker-api"`; non-Sessionize sources are deadline-of-record; entries with no `source` infer it from the `cfp_url` host (written back in Step 8). Rules + inference table + backfill: `references/source-routing.md`.

### Sessionize-sourced

Two deterministic helpers bracket a single batched MCP call — the agent does not derive slugs, infer sources, join results, or pick verdicts in prose.

**1. Prepare the batch.** Pass the entries to verify on stdin as a JSON array — one object per new candidate (Steps 2–4) and per stored `open`/`approved` row — each `{id, cohort: "new"|"stored", cfp_url, source?, slug?}`:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/prepare-sessionize-batch.py
```

It routes by effective source (explicit `source`, else the `cfp_url` host inference shared with `backfill-source.py`), derives each Sessionize slug, and emits `{slugs, sessionize, non_sessionize, unverifiable, counts}` — routing and slug-derivation logic in the script docstring + `references/source-routing.md`. **Checkpoint:** `save prep` (this output). Send `non_sessionize` ids to the branch below; `unverifiable` ids (Sessionize-sourced but no derivable slug) get the verification-failure protocol.

**2. Batch-verify.** One MCP round-trip for the full cohort, not one call per slug:

```
mcp__nanoclaw__sessionize_get_events(slugs: <slugs from step 1>)
```

Returns one array, one entry per requested slug: `{slug, ...event fields}` or `{slug, error}`. **Checkpoint:** `save sessionize_results` (this array) — it is the one non-reproducible artifact in Step 5 (a live API response), so a continuation must reload it rather than re-issue the call.

**3. Apply results.** Pass `{"prep": <step-1 output>, "results": <step-2 array>}` on stdin:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/apply-sessionize-results.py
```

It joins each result to its entry by `slug` (fanning one result out to **every** entry sharing it) and emits one `decision` per entry — **checkpoint:** `save decisions` (this output) — the verdict predicates and the verbatim dismissal `bot_notes` live in the script. Apply each decision to the working set:
- `verified` → set `deadline` to the decision's value, mark `_verified_this_run: true`, clear the stale markers per `references/contracts.md` (`stale: false`, strip the canonical `⚠️ STALE DATA` prefix, drop `_verify_skipped`), and attach the decision's `event` fields (e.g. `expenses_covered`) in memory for Steps 6/8.
- `dismiss` → `status: "dismissed"`, `bot_notes` = the decision's `bot_notes`.
- `drop` → drop the new candidate.
- `verify_failed` → apply the verification-failure protocol in `references/contracts.md`.

### Non-Sessionize-sourced

No live API call — the source feed is the authority. Mark `_verified_this_run: true` on every entry in this branch (new candidates AND stored `open`/`approved`) so Step 8 advances `last_verified` to today. Stored entries additionally: set `stale: false`, strip any single leading `⚠️ STALE DATA — Sessionize verification failed on ` prefix from `bot_notes` (idempotent), and delete `_verify_failed` if previously set.

Step 5 covers the full cohort each run. See `references/contracts.md` "Budget-low is not a defer reason."

## Step 6 — Source routing, blocklist, and AI relevance analysis

**Tier 1 — javaconferences.org auto-approve:** `status: "approved"`, `bot_notes: "Auto-approved: javaconferences.org source"`.

**Tier 2 — Blocklist:** Check conference name (case-insensitive) against `_blocked_prefixes`. Match → `status: "dismissed"`, `bot_notes: "Auto-dismissed: blocked prefix '[prefix]'"`.

**Tier 3 — AI relevance analysis:** Analyze remaining CFPs using all available data — Sessionize description (ground truth), tags, past speakers, audience type, format. Read `/workspace/trusted/user_professional.md` for Baruch's topics and apply criteria from `/workspace/group/RELEVANCE-CRITERIA.md`.

- Sessionize description available → use as ground truth.
- No description and ambiguous name → targeted web search before deciding.
- Sessionize-sourced candidates → lean relevant when topic is ambiguous; dismiss only if description clearly shows irrelevance.

Relevant → `status: "open"`, `bot_notes` citing specific evidence. Irrelevant → `status: "dismissed"`, `bot_notes: "Dismissed: [reason]"`.

**The "lean relevant when ambiguous" latitude applies ONLY when Tier 3 actually ran on the candidate.** Tier 3 covers every candidate that reaches it; see `references/contracts.md` "Budget-low is not a defer reason."

**Priority interest tagging (prefilter → arbitrate).** First check the policy: if `/workspace/group/cfp-priorities.json` is absent, empty, or carries no `priority_interests` (no policy), delete `matched_interests` from every non-`user_actioned` `open`/`approved` entry you process and skip the rest of this paragraph — no policy ⇒ pin everything. (Don't infer "no policy" from an empty prefilter result; a present policy that simply matched nothing also returns no proposals.)

Otherwise, pass every candidate now `open`/`approved` (JSON array of `{name, source, bot_notes}`) on stdin to the deterministic prefilter:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/match-priorities.py --priorities /workspace/group/cfp-priorities.json
```

If the prefilter exits non-zero (malformed config → exit 1, malformed records → exit 2), surface its stderr diagnostic and skip priority tagging this run — leave existing `matched_interests` untouched (don't tag, don't clear). On success it returns a JSON array parallel to the input (each `{name, proposed_interests}`, same order — join back by position). Then arbitrate per candidate, reading each proposed interest's definition in `cfp-priorities.json`: drop a proposal the interest's `note` excludes or the description contradicts; add an interest the CFP clearly matches on content with no hit (e.g. "Confitura" → `java`). Record the result as `matched_interests` — no match → `[]`. Never set, change, or delete `matched_interests` on `user_actioned: true` entries. Prefilter matching rules: `match-priorities.py` docstring. `note` semantics, absent-vs-`[]`, brief partitioning: `references/state-management.md`.

## Step 7 — Travel conflict check

1. Load `/workspace/group/travel-schedule.json`, extract `type: "Trip"` entries.
2. For each `open`/`approved` CFP, parse `conf_date`:
   - Parseable range → extract exact start/end.
   - Month-year only → search for exact dates. If not found, append `"Could not verify travel conflict — exact conference dates unknown."` to `bot_notes`.
3. Overlap with any Trip → `status: "conflict"`, append `"Travel conflict: overlaps with [Trip Name] ([start] – [end])."` to `bot_notes`.

**Checkpoint:** the working set is now fully decided (verification + relevance + travel applied). `save working_set` (the in-memory entry set) before the Step 8 write — a continuation here reloads it and writes, skipping Steps 2–7.

## Step 8 — Write to cfp-state.json

**Pre-write: dedup by URL.** Run the dedup script against on-disk state to collapse any two entries whose `cfp_url` normalises to the same `<host><path>` (lowercase host, scheme/query/fragment dropped, trailing `/` stripped):

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/dedup-by-url.py
```

Winner-selection priority (earlier wins): a) `user_actioned: true`; b) `shown_in_brief: true`; c) `source` matches URL's host; d) alphabetically-earliest slug. Skips collision group entirely when ≥2 `user_actioned` entries share one URL (surfaces on stderr).

Then for in-memory candidates from Steps 2–4, invoke `--lookup` mode:

```bash
printf '%s\n' "<candidate-1.cfp_url>" "<candidate-2.cfp_url>" ... \
  | python3 /home/node/.claude/skills/tessl__check-cfps/scripts/dedup-by-url.py --lookup
```

Reads newline-separated URLs from stdin; emits `{<input_url>: <existing_slug_or_null>}` JSON. For every non-null value, rewrite the candidate's key in the in-memory list to that existing slug. Idempotent.

Then apply priority rules (earlier wins):

1. **`user_actioned: true`** — preserve the entry's decision + metadata fields untouched: the bot does not refresh `updated`/`last_verified` (rules 5/6 apply only to entries actively written this run, not to preserved `user_actioned` ones) and does not re-tag `matched_interests`. The ONLY field stamped on these is `schema_version` (owner metadata, rule 9).
2. **Sticky (`shown_in_brief: true`)** — preserve `status` and `bot_notes`. Allowed updates: `deadline`, `city`, `conf_date`, `updated`, `last_verified`, `stale` + `⚠️ STALE DATA` prefix. Exception: Step 5 confirmed closed or online overrides stickiness.
3. **Existing `open`/`approved` without sticky** — update status, `bot_notes`, metadata. Downgrade-to-dismissed MUST set `status: "dismissed"`.
4. **New entries** — write status and `bot_notes` from Steps 6–7. Inherit `_verified_this_run: true` from Step 5. New entries that fail Sessionize verification are dropped.
5. Set `updated` to today on every written entry.
6. Set `last_verified` to today for every `_verified_this_run: true` entry.
7. `_verify_failed: true` AND status still `open`/`approved`: persist `stale: true` and prepend the canonical stale prefix per `references/contracts.md` (idempotent). Cleared on next successful verification.
8. Persist `matched_interests` from Step 6 on every `open`/`approved` entry it tagged this run. When Step 6 cleared it (priorities config missing/empty), delete the field from those entries; preserve the prior value untouched on `user_actioned: true` entries.
9. Do NOT hand-stamp `schema_version`. After the state write, run the deterministic stamper — the single source of stamping (owner migration per `references/state-management.md` "Schema version & ownership"):

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/stamp-schema-version.py
   ```

   It stamps `schema_version: 1` on EVERY record (incl. `user_actioned`, `dismissed`, `sent`, `remind`), idempotently, and rewrites the file only when something changed. Output: `{"total": M, "stamped": N}`. A non-zero exit means the state file is missing/unreadable — surface it.

10. Do NOT hand-write the top-level `_last_checked`. After stamping schema versions, run the deterministic freshness stamper — the single writer of `_last_checked`:

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/stamp-last-checked.py
   ```

   It sets the top-level `_last_checked` to the run timestamp unconditionally (a run that changed no record still checked, so the heartbeat must advance), touching nothing else. Output: `{"_last_checked": "<iso>"}`. A non-zero exit means the state file is missing/unreadable — surface it. Freshness lives here, not in per-record `updated`: read `_last_checked` to tell "pipeline ran" from "data unchanged."

11. The run completed successfully — clear the resume checkpoint store so the next run starts fresh:

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py done
   ```

   Only here, after the state write and both stampers succeeded. If an earlier step failed and you stopped, do NOT clear — the saved stages let a same-day retry resume (`references/run-state.md`).

After writing cfp-state.json, emit the run's verification report inside an `<internal>` block:

```
<internal>
{"checked_at": "<ISO>", "new_candidates_added": N, "existing_verified": N, "existing_verify_failed": N}
</internal>
```

## Step 9 — Sort and format output

**Stale-data guardrail (applied before formatting).** Suppress an entry from the brief if:
- `_verify_failed: true`, OR
- `last_verified` is missing or >7 days ago, OR
- No slug (manual entry) without a human-written, fresh `last_verified` with provenance in `notes`.

Stickiness locks in relevance verdicts, not deadline freshness. Suppression is logged to `/workspace/group/cfp-suppressed-today.json`.

**Urgency claims require fresh verification.** Only output deadline urgency emphasis (≤48h) when `_verified_this_run` is true and `cfp_end_local` is within 48h. Otherwise use plain `CFP closes [deadline]`.

Sort `open`/`approved` CFPs by deadline. Group by urgency:
- 🔴 ≤3 days
- 🟡 4–7 days
- 🟢 8–31 days
- ⬜ >31 days

Format:
```
[emoji] <b>[Conference Name]</b> — [City, Country], [Conference Date]
  CFP closes [deadline] ([N days])
  Submit: [URL]
  [bot_notes — one line]
```

If no open/approved CFPs: return nothing (wrap in `<internal>`).

## Step 10 — Output

Return the formatted, grouped list. Include a brief note if any data sources were unavailable. Dismissed and conflict CFPs are not shown.

If `existing_verify_failed > 0`, append a short user-visible warning naming the count and the resulting `⚠️ STALE DATA` entries.

## State Management

See `references/state-management.md` for status values, slug format, user-feedback action table, calibration rules, and state-format example. Schema: `/workspace/group/cfp-state.json`; criteria: `/workspace/group/RELEVANCE-CRITERIA.md`.
