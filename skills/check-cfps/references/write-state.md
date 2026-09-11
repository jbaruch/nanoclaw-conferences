# State-write procedure

Required procedure for check-cfps Step 8, in both scheduled and interactive modes. Execute in order; return to Step 8 for the internal report. Paths beginning `references/` are relative to the owner skill directory.

**Pre-write: dedup by URL.** Run the dedup script against on-disk state to collapse any two entries whose `cfp_url` normalises to the same `<host><path>` (lowercase host, scheme/query/fragment dropped, trailing `/` stripped):

```bash
python3 /home/node/.claude/skills/tessl__check-cfps/scripts/dedup-by-url.py
```

Winner selection, source-priority ranking, and merge-field inheritance are the script's contract — see the `scripts/dedup-by-url.py` docstring; do not re-derive them in prose. What the skill relies on: `user_actioned` entries always win and are never mutated, and priority-bearing source attribution plus the `name` survive the merge (jbaruch/nanoclaw-conferences#23/#25). Groups the script refuses to resolve are reported in `skipped_multi_user_actioned` (stderr detail) — surface them for manual review.

Then for EVERY in-memory entry you are about to write — new candidates from Steps 2–4 AND stored rows carried through Steps 5–7 — invoke `--lookup` mode:

```bash
printf '%s\n' "<entry-1.cfp_url>" "<entry-2.cfp_url>" ... \
  | python3 /home/node/.claude/skills/tessl__check-cfps/scripts/dedup-by-url.py --lookup
```

Reads newline-separated URLs from stdin; emits `{<input_url>: <existing_slug_or_null>}` JSON. For every non-null value, rewrite that entry's key in the in-memory set to the returned slug. This matters for stored rows too: the dedup pass above may have deleted a stored row's slug as a duplicate, and writing it back under its old key would resurrect the duplicate the dedup just removed (jbaruch/nanoclaw-conferences#24). If the rewrite makes two in-memory entries share a slug, they are both updates to that one state row — apply the priority rules below once for that slug. Idempotent.

Then apply priority rules (earlier wins):

1. **`user_actioned: true`** — preserve the entry's decision + metadata fields untouched: the bot does not refresh `updated`/`last_verified` (rules 5/6 apply only to entries actively written this run, not to preserved `user_actioned` ones) and does not re-tag `matched_interests`. The ONLY field stamped on these is `schema_version` (owner metadata, rule 10).
2. **Sticky (`shown_in_brief: true`)** — preserve `status` and `bot_notes`. Allowed updates: `deadline`, `city`, `conf_date`, `updated`, `last_verified`, `stale` + `⚠️ STALE DATA` prefix. Exception: Step 5 confirmed closed or online overrides stickiness.
3. **Existing `open`/`approved` without sticky** — update status, `bot_notes`, metadata. Downgrade-to-dismissed MUST set `status: "dismissed"`.
4. **New entries** — write status and `bot_notes` from Steps 6–7. Inherit `_verified_this_run: true` from Step 5. New entries that fail Sessionize verification are dropped.
5. Set `updated` to today on every written entry.
6. Set `last_verified` to today for every `_verified_this_run: true` entry.
7. `_verify_failed: true` AND status still `open`/`approved`: persist `stale: true` and prepend the canonical stale prefix per `references/contracts.md` (idempotent). Cleared on next successful verification.
8. Persist `matched_interests` from Step 6 on every `open`/`approved` entry it tagged this run. When Step 6 cleared it (priorities config missing/empty), delete the field from those entries; preserve the prior value untouched on `user_actioned: true` entries.
9. **Commit through the lock-owning writer — never write cfp-state.json directly.** Pipe the finished working set (JSON object of `slug → record`, `_`-prefixed keys excluded) to the committer, which applies it as per-slug replacements under the shared advisory lock (jbaruch/nanoclaw-conferences#35):

   ```bash
   printf '%s' '<working-set json>' | python3 /home/node/.claude/skills/tessl__check-cfps/scripts/commit-state.py
   ```

   Concurrent writers' updates to other slugs survive, and `user_actioned: true` is re-checked on disk at commit time so a mid-run user action is never overwritten — surface a non-zero `skipped_user_actioned` in the run report. Payload validation, the `_`-key refusal, and the output shape are the script's contract (`scripts/commit-state.py` docstring). Abort on non-zero exit.

10. **Post-write dedup guard.** After the state write, re-run `dedup-by-url.py` (same invocation as the pre-write pass). This is the deterministic backstop against duplicate resurrection: if any write re-created a slug the pre-write dedup had merged away, this pass collapses it again before the stampers run, so on-disk state never ends a run with two slugs for one CFP (jbaruch/nanoclaw-conferences#24). A clean run reports `slugs_dropped: 0`; a non-zero count means the lookup rewrite above was missed — surface it in the run report.

11. Do NOT hand-stamp `schema_version`. After the state write, run the deterministic stamper — the single source of stamping (owner migration per `references/state-management.md` "Schema version & ownership"):

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/stamp-schema-version.py
   ```

   It stamps `schema_version: 1` on EVERY record (incl. `user_actioned`, `dismissed`, `sent`, `remind`), idempotently, and rewrites the file only when something changed. Output: `{"total": M, "stamped": N}`. A non-zero exit means the state file is missing/unreadable — surface it.

12. Do NOT hand-write the top-level `_last_checked`. After stamping schema versions, run the deterministic freshness stamper — the single writer of `_last_checked`:

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/stamp-last-checked.py
   ```

   It is **evidence-gated** (jbaruch/nanoclaw-conferences#8): it advances `_last_checked` only when the `verify-sessionize.py` driver left a `verify-evidence.json` marker for this run showing ≥1 entry resolved from a live response (or there was nothing to verify). Output on a clean stamp: `{"_last_checked": "<iso>", "verification": "live"|"none-required"}`, exit 0. If verification did not happen (driver skipped, or a total Sessionize outage), it does NOT advance the heartbeat — it writes `_last_checked_skipped` and **exits 3**: treat that exit like a stamp failure (do NOT proceed to clear the checkpoint in item 13; report a skipped-verification run). On exit 3, ALSO invalidate the verification stages so a same-day retry re-runs Step 5 live instead of resuming the failed evidence (jbaruch/nanoclaw-conferences#31):

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py invalidate verify working_set verify-evidence
   ```

   Earlier stages (`fetch`, `candidates`) stay checkpointed — only the failed verification and everything downstream of it re-runs. Exit 1 means the state file is missing/unreadable — surface it. Freshness lives here, not in per-record `updated`.

13. The run completed successfully — clear the resume checkpoint store so the next run starts fresh:

   ```bash
   python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py done
   ```

   Only here, after the state write and both stampers succeeded — and only if the freshness stamper (item 12) exited 0. If the stamper exited 3 (verification not evidenced) or an earlier step failed and you stopped, do NOT clear — the saved stages let a same-day retry resume (`references/run-state.md`).
