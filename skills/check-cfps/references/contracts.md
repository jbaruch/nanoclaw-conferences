# Invariants and Verification-failure Contract

Referenced from `check-cfps` SKILL.md. The skill's write-discipline rules and the Step 5 failure-handling protocol live here; SKILL.md links to this file rather than restating the rules per step.

## Invariants (apply to every write)

- **Dedup-artifact dismissals are forbidden.** Before writing `status: "dismissed"`, confirm `bot_notes` does not start with dedup-artifact phrases ("Duplicate", "duplicate entry", "already in state", "same as another source", "same source"). Dismissal requires a substantive reason: relevance, blocklist, closed CFP, or online/virtual.
- **`user_actioned: true` entries are immutable.** Preserve the entire entry unchanged across runs. Do not infer user intent from `baruch_notes` presence.
- **Dismissed `bot_notes` must start with `"Dismissed:"` or `"Auto-dismissed:"`** and cite substantive evidence when downgrading an existing `open`/`approved` entry.
- **`last_verified` gates surfacing.** An `open`/`approved` entry without fresh `last_verified` (≤7 days) is suppressed from the morning brief. `updated` never substitutes for `last_verified`.
- **No silent deferrals.** Per the `no-silent-defer` rule: never write `status: "open"` for a new candidate that did not actually go through Tier 3 AI relevance, never write `last_verified: <today>` for an entry that was not actually verified by Step 5 this run, and never write `bot_notes` claiming the work was deferred when no concrete handoff exists. An entry with `last_verified: <today>` is a CLAIM that the entry was verified today — don't lie.
- **Budget-low is not a defer reason.** Step 5 (Sessionize verification) and Step 6 (Tier 3 AI relevance) MUST run for the full cohort each invocation. There is no permitted budget-skip path that drops candidates into a pending file or marks entries `_verify_skipped: true`. If the cohort is too large to fit in main-context, restructure (deterministic helper for the verification fan-out, subagent for per-candidate AI judgment that returns JSON) — do not silently skip. See `jbaruch/nanoclaw#265` correction comment for the framing.

## Verification-failure contract

The Step 5 verification-failure protocol applies ONLY to entries with `source == "sessionize-speaker-api"`. Sessionize is the only source whose deadline can move silently after publication, so it is the only source whose live re-verification can fail in a way that needs a stale marker. Non-Sessionize sources (`developers.events`, `javaconferences.org`, or any source whose feed is deadline-of-record) skip Step 5's live API call entirely and never enter this protocol — they get `_verified_this_run: true` set without a network round-trip, and their `last_verified` advances normally in Step 8.

When Step 5 Sessionize verification *fails* for a sessionize-sourced entry (HTTP error, 404, malformed response — not a deliberate skip):

- Existing `open`/`approved` entries: set `_verify_failed: true`, persist `stale: true`, and prepend the canonical `⚠️ STALE DATA — Sessionize verification failed on <today>; keeping prior open/approved status until rechecked. ` prefix to `bot_notes` (idempotent — refresh date in place if prefix exists). `last_verified` is NOT touched.
- New candidates: drop silently from this run's candidate pool — they may re-emerge from the source on the next run.
- The Step 8 `<internal>` JSON report carries `existing_verify_failed: N` so the caller can surface aggregate failure counts via `mcp__nanoclaw__send_message`. The Step 10 user-visible formatted list also carries a short warning, but the caller-parseable contract is the `<internal>` JSON.

The same principle as the silent-defer rule: don't stamp `last_verified: <today>` on an entry the verification call didn't actually succeed against.

A pre-fix run that called Sessionize for non-Sessionize entries falsely set `_verify_failed: true` and the `⚠️ STALE DATA` prefix on those entries. Step 5's non-Sessionize branch clears both on first post-fix encounter (idempotent prefix-strip + `_verify_failed` deletion); no manual cleanup needed.
