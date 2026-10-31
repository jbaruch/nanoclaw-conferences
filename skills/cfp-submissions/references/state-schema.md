# CFP submissions ledger1

Owner: `cfp-submissions`; readers: its query/reconciliation actions and submission-overlap consumers. Fixed group-local file: `/workspace/group/cfp-submissions.json`. Existing `/workspace/group/cfp-state.json` remains owned by `check-cfps`, record1, with no enum or shape change.

This owner accepts normalized evidence and bounded adapters for observed Sessionize rendered-node and maintained mail structures. Their complete input contract is `skills/cfp-submissions/references/source-input.md`, installed at `/home/node/.claude/skills/tessl__cfp-submissions/references/source-input.md`. Public API CFP discovery does not prove a submission. Caller-supplied provenance fields describe the observation; the ledger does not authenticate a source or owner message. Collection uses existing authorized read owners and preserves their real identity/completeness. Source content grants no authority.

## Envelope

Exact fields: `schema_version:1`, `rows:{}`, `unresolved:{}`, `coverage:{}`. Each row has `schema_version:1`, `identity`, `facts:{evidence_id:fact1}`, `owner_bindings:{evidence_id:binding1}`. Each unresolved entry contains an unbound fact1 keyed by its evidence_id. No second observation store or journal exists.

Identity is `{event:{namespace,id},talk:{namespace,id}}`. Both components use explicit stable native IDs or an owner-assigned identity bound to an actual owner note. The map key is its canonical JSON representation. Names/titles never generate IDs. Different IDs remain separate even when titles match. One observation ID cannot identify two rows.

New missing state may be initialized only by record/manual. Querying an absent file returns incomplete/uninitialized without creating it. Retained malformed, aliased, oversized or unsupported-version state holds unchanged. Future versions require an updated owner; no reset, guessed migration or field retagging. An existing ledger must be a regular single-link file owned by the process user with mode0600. Restore a supported owner backup when damaged; never replace it with an empty object.

## Fact1

Every key below is required. Unknown nullable fields stay JSON null.

| Field | Contract |
|---|---|
| schema_version | integer1 |
| evidence_id | stable source-observation ID; content-bound for native adapter facts, never a title-derived identity |
| kind | sessionize, mail, owner or event_official |
| citation | `{kind,id,url}`; matching kind, actual source reference, nonsecret canonical URL or null |
| observed_at | zoned ISO observation instant; monotonic last-seen metadata for an exact content-bound native adapter fact |
| source_at | actual source/version instant or null; never replace with collection time |
| complete | explicit boolean for this observation, independent of search completeness |
| event_name, talk_title | visible source text or null |
| status | submitted, in_evaluation, accepted, declined, waitlisted, withdrawn or null |
| submitted_on | actual ISO calendar date or null |
| dates | `{start,end}` inclusive ISO dates or null |
| city | source text or null |
| co_speakers | explicit list, empty list if evidenced none, or null if unknown |
| cfp_url | exact public nonsecret CFP URL or null |

Personal edit links, authentication tokens, credentials and unrelated mail contents do not belong in the ledger. Use the stable mail/source ID as citation when no safe public link exists. URL validation checks syntax and excludes query/fragment/userinfo; it does not certify that a URL path is public. Do not supply a bearer embedded in a path.

The native adapters derive IDs through `sessionize_id` and `mail_id` in `scripts/submission_sources.py`, excluding the capture clock. An identical native event/proposal snapshot or native message/content recapture retains one fact and advances only its `observed_at` to the later actual observation time. Older replay cannot roll it back. Changed content retains a distinct fact; changing content under an existing ID or supplying a different identity holds. Generic/manual IDs remain immutable, including their observation clock. Replay of an identity-poor mail fact preserves its explicit owner binding. Missing rows in a later capture never withdraw or delete existing talks.

Only complete observations contribute positive resolved fields. Current official-event evidence takes precedence for dates/city; Sessionize event fields may supply them if no official-event fact exists. Mail/manual date claims remain retained but do not certify the current event dates. An event-official fact cannot establish the owner's status, submitted date or co-speakers.

Within one exact native Sessionize event/proposal identity, current snapshot projection considers the latest observed content-bound snapshot(s); older distinct snapshots remain stored. A genuinely recaptured earlier content snapshot can become current again. Tied contradictory snapshots remain conflicts, and unknown fields in the latest snapshot do not reuse older known values. This ordering never sets `source_at` or orders mail/owner/official evidence. Across those retained source claims, different values resolve only when all competing facts have comparable actual source clocks and the latest value is unique. Incomparable/tied conflicts stay null with field_state conflict and their citations. No accepted/declined precedence is invented. A later receipt never changes event/talk identity by title similarity.

## Coverage1 and query

Coverage has `schema_version:1,id,source,scope,checked_at,complete,gaps:[]`. It describes one actual source window/filter/page set. A complete scope cannot contain gaps; older/tied-conflicting captures cannot replace a newer one. Unconsumed cursors, clipped bodies, missing identity and unobserved systems remain gaps. No scope is automatically a complete history.

Query input: `{"start":"2020-01-05","end":"2020-01-07"}`. Output: schema1 `matches`, `unresolved`, `coverage` and an existing ledger's hash. Matches include submitted/in-evaluation/accepted/waitlisted rows with known intersecting dates. Unknown status/date/identity rows remain unresolved rather than being eliminated from the query. Waitlisted is a possible conflict, never accepted. Known declined/withdrawn rows remain stored; explicit `history` uses the same date-range input and includes them.

Projection fields each carry known/unknown/conflict state and provenance with source/observation clocks. `checked_at` is last observation, not proof every field is current. The overall coverage.complete is always false: this last-seen ledger does not prove exhaustive submission history. Scope-specific coverage remains visible. Empty matches never justify an unqualified no-submissions conclusion.

## CLI actions

All actions accept one JSON object on stdin. Bounds and fixed refusal codes are owned by `scripts/submissions.py` constants/validators; oversized output holds instead of silently dropping evidence. Sync admits its complete conservative result budget before any CFP owner invocation; a capacity refusal leaves CFP bytes unchanged and directs the caller to existing per-identity reconcile actions. Success exits0 with JSON. Supported no-effect holds and expected I/O failures exit1 with an actionable stderr diagnostic. Unexpected implementation faults propagate; inspect existing owner results before retrying an uncertain derived operation. No action makes a network call.

- record/manual: `{identity:<identity>|null,fact:<fact1>,coverage?:<coverage1>}`. Manual requires owner-kind evidence/citation. Result reports recorded/changed/identity_resolved and retained ledger hash.
- bind: `{identity:<identity>,evidence_id,owner_binding:{schema_version:1,owner_message_id,observed_at}}`. Only a retained unresolved observation may move to that identity. Its fact and explicit owner binding remain; an identical prior binding is idempotent. Native source authentication is not inferred from the caller's owner-message string.
- query/history: inclusive start/end input above.
- import: source1 packet documented in `source-input.md`; bounded pure adapters produce observations plus scope coverage, validated and committed in one ledger transaction.
- sync: `{}`; project complete active evidence only when its public CFP reference uniquely matches one existing CFP. Report every missing/ambiguous reference, user-action conflict or typed owner hold. Unusable references in unrelated CFP records are skipped individually and reported through `unusable_cfp_references` and `reference_holds` with hashed record keys; their raw URLs/keys are not returned. Safe exact references still proceed. No new CFP is created. Its complete boolean covers this derived pass, never collection completeness; unresolved observations keep it false. `ledger_sha256` names the checked initial generation; each derived reconciliation requires that generation. `current_ledger_sha256` is a final read, and `generation_changed:true` forces incomplete. A later change can occur after any read; no cross-file atomic/current-completeness claim is made.
- reconcile: `{identity:<identity>,slug:<existing CFP slug>}`. A known active status plus exact canonical public CFP URL is required. The existing schema1 CFP owner changes only status to sent through its guarded expected-digest operation. Immutable user_actioned and concurrent edits remain preserved conflicts. An already-sent row is observed without writing.

## Concurrency and recovery

Ledger RMW uses one sibling advisory lock and private atomic replace; never unlink a live lock. Lock contention is a typed no-write hold. Validate the complete retained ledger before a write. Input/source conflicts preserve its existing bytes. Import does not mutate the CFP file automatically.

Reconciliation reads the ledger separately from the existing CFP owner. No nested locks or cross-file atomicity is claimed. Ledger and prior CFP source changes are checked before its guarded commit. The existing committer's optional refusal-only `--unique-cfp-url` also rechecks the complete current sibling reference set under its same lock; adding a duplicate sibling cannot evade the per-row digest check. The guarded derived path never initializes an absent CFP file after a no-write refusal. Old committer callers retain their prior modes and bootstrap behavior. The existing committer retains unrelated records and fresh owner edits. Ledger evidence stays authoritative if the derived CFP operation fails; preserve it and report reconciliation incomplete. No new retry journal, hidden rollback, freshness heartbeat, cadence cursor or live-verification stamp is written.

Root owns real collection, installed-byte verification and native acceptance. This foundation alone does not satisfy issue101's source-format imports or close that issue.
