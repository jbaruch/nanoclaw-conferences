---
name: check-cfps
description: Finds open CFPs relevant to Baruch across Java/AI/developer conferences and maintains persistent CFP state (sent/dismissed/remind) in cfp-state.json. Use when Baruch asks about upcoming conferences, call for papers, speaking opportunities, CFP deadlines, or where to submit a talk proposal.
---

# Check CFPs (with State Management)

Process steps in order. Do not skip ahead.

Fetches open CFPs from multiple sources via `scripts/check-cfps-fetch.py`, applies routing + AI-based relevance analysis in Step 6, and maintains persistent state across sessions. The fetcher owns source-list and blocklist filtering; tier-based routing (including the javaconferences.org auto-approve path) is the agent's work in Step 6.

## Scheduled execution

- When invoked by `tessl__nightly-cfp-sync` or with `scheduled` arguments, apply scheduled mode throughout this invocation, including resumed runs.
- In scheduled mode, use the co-shipped fetch and verification scripts and local files only. Do not call `WebSearch`, `WebFetch`, `mcp__nanoclaw__fetch_markdown`, browser-rendering tools, or delegate web research.
- Apply the scheduled branches in Steps 4, 6, and 7. Complete verification, relevance decisions, state writes, stampers, suppression logging, and the internal report in this invocation.
- Never defer mandatory work to a later turn. A denied required tool is a technical failure: report it to the caller and finish without claiming success.
- Direct interactive invocations retain web research. Scheduled mode takes precedence over any web-fallback instruction in fetched warnings or references.

## Contracts

The skill's write invariants (dedup-artifact ban, immutable `user_actioned`, dismissal-reason discipline, `last_verified` surfacing gate, no-silent-defer, budget-low-is-not-a-defer-reason) and the Step 5 verification-failure protocol (`_verify_failed`, `⚠️ STALE DATA` prefix, caller-visible counts) live in `references/contracts.md`. Read once, apply throughout.

## Step 1 — Resume guard

Run this first, before any other step. This pipeline can be interrupted mid-run by a token-limit continuation. To resume from disk instead of reconstructing the working set from chat history, open (or start) the run's checkpoint store:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py begin
```

- `{"resume": false}` — fresh run. Proceed from Step 2.
- `{"resume": true, "completed": [...]}` — a run begun earlier today was interrupted. For each stage already in `completed`, reload its artifact with `run-state.py load <stage>` instead of recomputing it, and resume at the first step whose stage is absent.

**Scheduled override:** after `begin`, invalidate prior pipeline artifacts, ignore its saved `completed` list, and proceed from Step 2. Abort on a non-zero exit. Initialize this invocation's `research_warnings` to an empty array.

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py invalidate fetch candidates verify working_set verify-evidence
```

Stages, in pipeline order: `fetch` (Step 3), `candidates` (Steps 2–4 merge), `verify` (Step 5 driver), `working_set` (Steps 5–7, ready for Step 8). After producing each stage's artifact, persist it:

```bash
echo '<artifact json>' | python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py save <stage>
```

Resume is best-effort — stages are idempotent and Step 5 re-verifies the full cohort, so a fresh run is always safe; the store only avoids redoing expensive work. It is per-UTC-day (a continuation on a later day resets). Stage shapes, lifecycle, and the day-boundary reset: `references/run-state.md`.

## Step 2 — Sessionize speaker API candidates

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/discover-open-cfps.py
```

Discovers new Sessionize open-CFP candidates deterministically (needs the host-injected `SESSIONIZE_SPEAKER_KEY`; reads `/workspace/group/cfp-state.json` to skip already-tracked slugs). Do NOT call the Sessionize API or parse its response inline — that is the script's job (jbaruch/nanoclaw-conferences#9). Parse stdout `{candidates, counts}` and carry `candidates` into the pool. Abort if the script exits non-zero (an outage must not read as "0 new CFPs"). Do not write to state here. The candidate shape and filter rules are the script's contract (`scripts/discover-open-cfps.py` docstring).

## Step 3 — Run fetch-and-filter script

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/check-cfps-fetch.py
```

Parse JSON output: `cfps`, `warnings`, `checked_at`. **Checkpoint:** `save fetch` (the script's stdout) before merging. Then merge Sessionize candidates from Step 2, dedup by slug. Tier-1 auto-approve is NOT guaranteed on name collisions; where you must choose between equivalent rows, keep the one with more complete metadata. Surface `warnings` at the top of output. Abort if script fails.

## Step 4 — Web search for gaps

**Scheduled:** use the candidate pool from Steps 2–3 without web gap search. Continue to the checkpoint below, including when a fetch warning suggests web fallback. If both primary feeds report fetch or format failures, report a technical failure and finish here; an empty valid feed is not a failure.

**Interactive:** read `/workspace/trusted/user_professional.md` for Baruch's current speaking topics. Construct 2–3 web search queries from his actual topics combined with CFP discovery terms. Add new CFPs not already in the list (dedup by conference name). Apply hard filters (no online/virtual, no excluded locations). Do not apply relevance filtering yet.

**Checkpoint:** once the full candidate pool is assembled (Steps 2–4 merged and deduped), `save candidates` (the merged pool) before Step 5.

**Interactive JS-rendered CFP pages:** use the fallback chain in `references/web-fetch-fallback.md` for Steps 4, 6, and 7. Do not use that chain in scheduled mode.

## Step 5 — Source-aware verification

**Pre-verify: name repair.** Before assembling the stored cohort, run the deterministic name backfill so no nameless record reaches Step 6 blind (a record without `name` is invisible to the priority matcher and the brief — jbaruch/nanoclaw-conferences#23):

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/backfill-name.py
```

It guarantees a usable `name` on every record it can, touching nothing else and never touching `user_actioned: true` entries (immutability per `references/contracts.md`); the derivation rules are the script's contract (`scripts/backfill-name.py` docstring). Surface a non-zero `unnamed_remaining` or `skipped_user_actioned` in the run report. Abort on non-zero exit (state file unreadable).

**Pre-verify: deadline expiry.** Then run the deterministic expiry pass — the single writer of `status: "expired"` (jbaruch/nanoclaw-conferences#27):

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/expire-cfps.py
```

It expires stale non-Sessionize `open`/`approved` rows whose deadline has passed, so they leave the verify cohort below instead of being re-blessed every run; eligibility, guards, and the revival path are the script's contract (`scripts/expire-cfps.py` docstring). Include a non-zero `expired` count in the run report. Abort on non-zero exit (state file unreadable).

Verify two cohorts:
- **New candidates** from Steps 2–4.
- **Already-stored `open`/`approved` entries** — every slug in `cfp-state.json` with `status in (open, approved)`.

**Routing is source-aware** — Sessionize is authority only for `source == "sessionize-speaker-api"`; non-Sessionize sources are deadline-of-record; entries with no `source` infer it from the `cfp_url` host (written back in Step 8). Rules + inference table + backfill: `references/source-routing.md`.

### Sessionize-sourced

One deterministic driver does prepare → live per-slug verification → apply in a single invocation, calling the Sessionize API itself (host-injected `SESSIONIZE_EVENT_API_KEY`). Make the Sessionize round-trip ONLY through this script — never inline — so its large response stays out of context; do not derive slugs, join results, or pick verdicts in prose.

Pass the entries to verify on stdin as a JSON array — one object per new candidate (Steps 2–4) and per stored `open`/`approved` row — each `{id, cohort: "new"|"stored", cfp_url, source?, slug?}`:

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/verify-sessionize.py
```

It verifies the Sessionize cohort against the live API (host-injected `SESSIONIZE_EVENT_API_KEY`) and writes the `verify-evidence.json` marker Step 8's stamp reads, emitting `{prep, results, decisions, summary, non_sessionize, evidence}` — the routing, verdict rules, and per-slug failure contract are the script's (`scripts/verify-sessionize.py` docstring). **Checkpoint:** `save verify` (this output). Send `non_sessionize` ids to the branch below. Apply each decision to the working set:
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
- No description and ambiguous name → interactive runs use targeted web search before deciding; scheduled runs decide from fetched metadata and local criteria, stating uncertainty in `bot_notes` without inventing evidence.
- Sessionize-sourced candidates → lean relevant when topic is ambiguous; dismiss only if description clearly shows irrelevance.
- Scheduled non-Sessionize candidates with insufficient topic evidence → record the name and uncertainty in `research_warnings`. For a new candidate, omit it from the state write without persisting a dismissal; keep it eligible for later discovery. For an existing row, preserve its prior relevance decision and notes while applying this run's verified metadata and travel result. Missing evidence alone never justifies a downgrade. Do not claim the conference is off-topic without evidence.

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
   - Month-year, missing, or unparseable dates → interactive runs search for exact dates; scheduled runs use exact dates only when available in fetched metadata.
3. Overlap with any Trip → `status: "conflict"`, append `"Travel conflict: overlaps with [Trip Name] ([start] – [end])."` to `bot_notes`.

Judge exact-date availability for the warning helper in Step 8. Leave warning-string updates to that helper; retain date interpretation and travel-overlap decisions here.

**Checkpoint:** the working set is now fully decided (verification + relevance + travel applied). `save working_set` (the in-memory entry set) before the Step 8 write — a continuation here reloads it and writes, skipping Steps 2–7.

## Step 8 — Write to cfp-state.json

Read and execute the full procedure at this path before continuing:

```text
skills/check-cfps/references/write-state.md
```

Resolve it as `references/write-state.md` relative to this installed skill. It owns the dedup passes, per-entry write priorities, lock-owning commit, schema stamp, evidence-gated freshness stamp, and checkpoint cleanup. Abort on a technical failure; preserve the checkpoint. On freshness-stamper exit 3, follow its invalidation path and report `verification: "skipped"`.

After writing cfp-state.json, emit the run's verification report inside an `<internal>` block. `verification` is the freshness stamper's verdict — `"live"`/`"none-required"` when it advanced `_last_checked`, or `"skipped"` when it exited 3 (no live verification this run):

```
<internal>
{"checked_at": "<ISO>", "new_candidates_added": N, "existing_verified": N, "existing_verify_failed": N, "verification": "live"|"none-required"|"skipped", "research_warnings": ["<candidate name: missing topic evidence>"]}
</internal>
```

`research_warnings` is empty when no candidate lacks topic evidence. It is a run-report field, never a persisted CFP field. Surface non-empty warnings in the output; the scheduled caller consumes them as specified in its own skill.

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
