# Source-aware routing

Step 4 verifies new candidates and stored `open`/`approved` entries against the right authority for each CFP's source.

- **Sessionize is authority ONLY for `source == "sessionize-speaker-api"`.** Non-Sessionize sources are deadline-of-record.
- **Source inference** — stored entries with no `source` field infer it from the `cfp_url` host: `sessionize.com` → `sessionize-speaker-api`; `developers.events` → `developers.events`; `javaconferences.org` → `javaconferences.org`; else unsourced (non-Sessionize branch). The inferred value is written back in Step 7.
- Backfill for legacy entries: `skills/check-cfps/scripts/backfill-source.py`.
