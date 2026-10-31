# Submission source1 inputs

Owner: `cfp-submissions/scripts/submission_sources.py`, called only by the co-shipped `submissions.py import` action. One bounded JSON stdin packet, no network calls. The owner validates all proposed facts, all retained state and all coverage before one ledger publication. Raw captures are private invocation input, not another durable store. Do not retain complete messages, raw DOM, private edit URLs or credentials in the ledger.

These adapters parse structures; they do not authenticate caller-supplied JSON, Main identity, a mailbox sender, a source clock or an owner message. Genuine collection remains with the installed Admin read owners and supported visible-browser owner. Runtime role/credential grants are unchanged. The schema reference is `skills/cfp-submissions/references/state-schema.md`, installed at `/home/node/.claude/skills/tessl__cfp-submissions/references/state-schema.md`.

## Sessionize

The installed `jbaruch/nanoclaw-host` rule `rules/sessionize-rendered-capture.md`
and complete `skills/host-contracts/references/sessionize-rendered-capture.md`
govern collection. Follow Step1's verified provider lookup in the submissions
skill; its fixed no-argument capture owner returns this packet unchanged.
Missing or held collection supplies no empty result or source-completeness proof.

Exact outer fields:

```json
{
  "schema_version": 1,
  "source": "sessionize",
  "observed_at": "2020-12-01T00:00:00Z",
  "capture": {
    "schema_version": 1,
    "origin": "https://sessionize.com",
    "path": "/app/speaker/events",
    "title": "My events - Speaker profile @ Sessionize.com",
    "nodes": [],
    "scope": "complete rendered node capture; no forms or hidden values"
  }
}
```

`nodes` is the complete rendered DOM tree in preorder. Parser limits are owned by `skills/cfp-submissions/scripts/submission_sources.py` — `MAX_NODES`, `MAX_MESSAGES`, `MAX_BODY`; the CLI input cap is `MAX_INPUT` in `skills/cfp-submissions/scripts/submissions.py`. Each node has `tag`, `parent` (a preceding integer index, root null), `classes` and direct visible `text`; contained non-edit links may additionally have `href`. Values/attributes from forms, hidden DOM, storage, cookies, credential/profile exports and query/fragment-bearing links are excluded by the collector. `link_omitted:true` may describe an intentionally omitted link; its missing target supplies no identity.

The captured account/filter, native event/proposal relationships, status recognition and explicit date grammar are owned by `skills/cfp-submissions/scripts/submission_sources.py` — `sessionize`, `native_path`, `STATUS` and `event_dates`. Supply the returned rendered tree unchanged; do not reconstruct rows or assign statuses in the model. The adapter returns native event/talk identities and preserves distinct same-title talks. Duplicate identities refuse.

Unknown status labels remain null, with a scope gap; they never become submitted/accepted by default. Missing or unsupported date grammar remains null. Wrong route/filter/account/tree or unbound proposal links refuse the capture instead of initializing an empty ledger. Event/session count mismatches and pagination controls make scope coverage incomplete. Count agreement proves only the captured Current-events view, never past, declined, external events or full account history. The visible account marker is fingerprinted in private scope metadata, not authentication or a fabricated stable provider account ID.

List dates are event-owned Sessionize observations; city, co-speakers and submitted date are absent in the captured list and stay null. A displayed talk time/room is not an event city or submission time. The capture clock is `observed_at`; it never becomes `source_at`. The adapter's content-bound IDs omit that clock. Identical recaptures retain one fact and refresh its actual last-seen time monotonically. Current snapshot ordering is restricted to this same verified adapter/native event/proposal identity; a tied contradiction or conflict with an incomparable other source remains a conflict. Prior snapshots remain in history. A current event-official observation can resolve dates/city separately through the normalized record action.

## Maintained mail evidence

Exact outer fields:

```json
{
  "schema_version": 1,
  "source": "mail",
  "collections": [
    {
      "family": "pretalx",
      "scope": "the actual bounded search/filter/window",
      "observed_at": "2020-12-01T00:00:00Z",
      "evidence": {
        "schema_version": 1,
        "threads": [],
        "next_page_token": null,
        "threads_omitted": 0,
        "complete": true,
        "errors": []
      }
    }
  ]
}
```

`evidence` is the maintained Admin search-mail schema1 envelope. Preserve each thread's `messages`, `complete`, `messages_omitted`; each message's actual `messageId`, `subject`, bound `internalDate`, complete sanitized `body` and `body_complete`. Keep other owner-returned fields unchanged in the private packet. A full-body helper without its identity-matched search date is insufficient; never substitute collection time. No continuation token or error text is copied into coverage: only finite gaps are retained. Collection and message limits are owned by the named script constants and the `mail` input validator. Split at actual collection boundaries without calling an incomplete scope complete.

`family` selects one of the source adapter's `FAMILIES` inputs; it supplies no authentication or claim by itself. Recognized template fields and unsupported cases are owned by `skills/cfp-submissions/scripts/submission_sources.py` — `mail_fields`, `oxford_claim` and `own_form_claim`. Preserve the source owner output; do not preclassify ambiguous prose or create a guessed proposal identity. Recognized confirmations/decisions produce cited claims; unmatched templates retain unknown status. Available historical mail date/city claims remain in facts and do not certify current event dates/city. Multiple generic confirmations are observations, not identified talk counts.

All current mail observations lack a verified native event/proposal pair and enter the unresolved map, with native mail ID citations and source clocks. URLs, HTML attributes, abstracts and unrelated mail contents never enter projected fields. Clipped bodies cannot contribute positive resolved fields. Search/thread/body incompleteness, unsupported templates and missing proposal identities remain coverage gaps. Every occurrence is parsed and contributes its own query-scope gaps before deduplication. Fixed observed template predicates recognize content independently of the query-family label. Identical native messages found in several families become one observation with compatible parsed fields and the earliest actual collection clock within that packet, independent of collection ordering. Conflicting content or parser fields refuse the complete import; a family tag alone never grants a claim. Across repeated imports the same native message/content retains one fact and advances its last-seen clock monotonically, preserving any explicit owner binding. Mail resolution still uses actual `source_at`; collection time supplies no mail precedence or distinct-submission proof.

## Owner/manual and CFP projection

Systems without mail use the existing `manual` action, not an invented parser or sender-based owner grant. Ground each event/talk pair in the actual explicit owner note and assign stable note-based identities; preserve stated co-speakers and unknown submitted date. Event-date prose in an owner note remains a claim until current event-owned evidence resolves it. No owner note is copied into public fixtures.

After a record/import/binding, run `sync` with `{}`. It uses only an already evidenced exact public CFP reference that uniquely matches an existing CFP, then delegates to the unchanged expected-digest committer. No similar-title/year join, new CFP creation, source-history reset or `user_actioned` override occurs. Missing references and conflicts remain explicit; the evidence ledger stays committed if the derived projection cannot complete. `complete` in this result describes that derived pass, never exhaustive source collection.

## Source fixture/provenance boundary

`tests/submission_fixture_builders.py` constructs fresh synthetic structural transcriptions of the observed tree/templates for each test. Original captures and source-to-fixture hashes/transformation notes remain in a private operator artifact. Public fixture people, events, titles, IDs, dates and links are replacements; originals, bearers and unrelated prose are excluded. Those offline tests prove parser behavior, not fresh account access, exhaustive history or native issue101 acceptance. Root owns real collection, installed-byte verification and acceptance.
