"""Unit tests for skills/check-cfps/scripts/dedup-by-url.py.

Locks down the script's documented contract:

  - URL normalisation: scheme / query / fragment dropped, host
    lowercased, trailing `/` stripped, missing/non-string input → None.
  - Winner selection per priority:
      a) `user_actioned: true` (immutability)
      b) `source` matches URL host (authoritative API row)
      c) alphabetically-earliest slug (deterministic tiebreak)
  - Multiple `user_actioned` entries on one URL → group skipped with
    stderr warning (manual review).
  - bot_notes merge: appended to winner with provenance prefix, only
    if loser's notes are non-empty AND not already a substring.
  - Winner is `user_actioned: true` → bot_notes NEVER touched
    (immutability invariant from references/contracts.md), losers
    still dropped.
  - Idempotent: second run is a no-op (no collisions remain).
  - Atomic write via sibling temp file + os.replace (matches
    backfill-source.py's pattern; concurrent containers run on the
    same /workspace/group/).
  - Missing state file → no-op exit 0.
  - Corrupt JSON / non-dict root → exit 1 with stderr diagnostic.
  - Underscore-prefixed config keys and non-dict entry shapes skipped.
"""

import json
from pathlib import Path

import pytest


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stdout_payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# normalise_url unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        # Lowercase host + path, scheme dropped.
        ("https://sessionize.com/foo", "sessionize.com/foo"),
        ("http://sessionize.com/foo", "sessionize.com/foo"),
        ("https://SESSIONIZE.COM/Foo-Bar", "sessionize.com/Foo-Bar"),
        # Query / fragment dropped.
        ("https://sessionize.com/foo?x=1", "sessionize.com/foo"),
        ("https://sessionize.com/foo#section", "sessionize.com/foo"),
        ("https://sessionize.com/foo?x=1#section", "sessionize.com/foo"),
        # Trailing slash stripped.
        ("https://sessionize.com/foo/", "sessionize.com/foo"),
        # Host-only.
        ("https://sessionize.com", "sessionize.com"),
        ("https://sessionize.com/", "sessionize.com"),
        # Invalid / missing inputs → None.
        ("", None),
        ("not-a-url", None),
        ("https://", None),
    ],
)
def test_normalise_url(dedup_by_url, url, expected):
    """Direct unit tests on the normalise_url helper — covers the
    documented normalisation rules without going through main()."""
    assert dedup_by_url.normalise_url(url) == expected


def test_normalise_url_non_string_inputs(dedup_by_url):
    """Non-string types (None, int, list, dict) all return None — the
    script never crashes on schema drift in the cfp_url field."""
    assert dedup_by_url.normalise_url(None) is None
    assert dedup_by_url.normalise_url(42) is None
    assert dedup_by_url.normalise_url(["url"]) is None
    assert dedup_by_url.normalise_url({"url": "x"}) is None


# ---------------------------------------------------------------------------
# source_matches_url_host unit tests
# ---------------------------------------------------------------------------


def test_source_matches_url_host_canonical_pairing(dedup_by_url):
    """sessionize-speaker-api matches a sessionize.com URL (root + subdomain)."""
    assert (
        dedup_by_url.source_matches_url_host("sessionize-speaker-api", "https://sessionize.com/foo")
        is True
    )
    assert (
        dedup_by_url.source_matches_url_host(
            "sessionize-speaker-api", "https://events.sessionize.com/foo"
        )
        is True
    )


def test_source_matches_url_host_mismatched_pairing(dedup_by_url):
    """A non-canonical source on a sessionize URL does NOT match — the
    point of the rule is to identify the authoritative-source row."""
    assert (
        dedup_by_url.source_matches_url_host("developers.events", "https://sessionize.com/foo")
        is False
    )


def test_source_matches_url_host_suffix_collision(dedup_by_url):
    """`notsessionize.com` must NOT match sessionize — the subdomain
    check requires a leading dot."""
    assert (
        dedup_by_url.source_matches_url_host(
            "sessionize-speaker-api", "https://notsessionize.com/foo"
        )
        is False
    )


def test_source_matches_url_host_empty_inputs(dedup_by_url):
    """Empty / non-string inputs → False (never accidentally match)."""
    assert dedup_by_url.source_matches_url_host("", "https://sessionize.com/foo") is False
    assert dedup_by_url.source_matches_url_host("sessionize-speaker-api", "") is False
    assert dedup_by_url.source_matches_url_host(None, "https://sessionize.com/foo") is False
    assert dedup_by_url.source_matches_url_host("sessionize-speaker-api", None) is False


# ---------------------------------------------------------------------------
# main() / dedup() — the headline scenarios
# ---------------------------------------------------------------------------


def test_missing_state_file_is_no_op_exit_0(dedup_by_url, tmp_path, capsys):
    """No state file at the configured path is a documented no-op:
    exit 0 with zero counters and stderr diagnostic."""
    state_path = tmp_path / "absent.json"
    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload == {
        "groups_merged": 0,
        "slugs_dropped": 0,
        "notes_merged": 0,
        "skipped_multi_user_actioned": 0,
        "merges": [],
    }
    assert "state file not found" in captured.err


def test_corrupt_json_exits_1(dedup_by_url, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err
    assert str(state_path) in captured.err


def test_root_not_dict_exits_1(dedup_by_url, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected dict" in captured.err


def test_no_collisions_is_no_op(dedup_by_url, tmp_path, capsys):
    """Every URL appears exactly once → no group has >1 slug → nothing
    is dropped, nothing is written, all counters zero."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
                "devoxx-fr-2026": {
                    "status": "open",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
            }
        ),
        encoding="utf-8",
    )
    mtime_before = state_path.stat().st_mtime_ns

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 0
    assert payload["slugs_dropped"] == 0
    assert payload["merges"] == []
    # No write happens when slugs_dropped == 0.
    assert state_path.stat().st_mtime_ns == mtime_before


def test_live_repro_codemotion_milan(dedup_by_url, tmp_path, capsys):
    """The exact 2026-05-13 production state shape: two entries with
    the same `cfp_url` (sessionize.com/codemotion-milan-26) but
    different dict keys. The sessionize-speaker-api row wins (source
    matches URL host); developers.events row is dropped; loser's
    bot_notes merged with provenance prefix."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "codemotion-milan-26": {
                    "status": "open",
                    "name": "Codemotion Milan",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/codemotion-milan-26",
                    "deadline": "2026-05-15",
                    "last_verified": "2026-05-13",
                    "bot_notes": "Italian dev conference; broad tech mix",
                },
                "codemotion-milan-2026": {
                    "status": "open",
                    "name": "Codemotion Milan 2026",
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/codemotion-milan-26",
                    "deadline": "2026-05-15",
                    "last_verified": "2026-05-13",
                    "bot_notes": "Listed on developers.events under the Italian feed",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 1
    assert payload["slugs_dropped"] == 1
    assert payload["notes_merged"] == 1

    state = _read_state(state_path)
    # The sessionize-speaker-api keyed row is the winner.
    assert "codemotion-milan-26" in state
    assert "codemotion-milan-2026" not in state
    # Loser's bot_notes is appended with provenance marker.
    winner = state["codemotion-milan-26"]
    assert "Italian dev conference; broad tech mix" in winner["bot_notes"]
    assert "[merged from codemotion-milan-2026]" in winner["bot_notes"]
    assert "Listed on developers.events under the Italian feed" in winner["bot_notes"]


def test_user_actioned_wins_and_remains_immutable(dedup_by_url, tmp_path, capsys):
    """When a colliding pair includes one `user_actioned: true` entry,
    that entry MUST be picked as the winner AND its fields MUST NOT
    be modified (immutability invariant from contracts.md). The
    loser is still dropped but no bot_notes merge happens."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "conf-a-2026": {
                    "status": "sent",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "User submitted on 2026-04-01",
                },
                "conf-b-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "AI-generated relevance reasoning",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 1
    assert payload["slugs_dropped"] == 1
    # No bot_notes merge because the winner is user_actioned.
    assert payload["notes_merged"] == 0

    state = _read_state(state_path)
    assert "conf-a-2026" in state
    assert "conf-b-2026" not in state
    # User's entry preserved verbatim.
    assert state["conf-a-2026"]["bot_notes"] == "User submitted on 2026-04-01"
    assert state["conf-a-2026"]["user_actioned"] is True


def test_sticky_winner_immutable_bot_notes(dedup_by_url, tmp_path, capsys):
    """`shown_in_brief: true` (sticky) entries must be picked as
    winner ahead of source-host-match per SKILL.md Step 7's sticky
    rule, AND their `bot_notes` must NOT be modified by the dedup
    merge — stickiness preserves `status` + `bot_notes` per
    `references/contracts.md`. The loser is still dropped but no
    notes-merge happens."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "sticky-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "shown_in_brief": True,
                    "last_shown_date": "2026-05-10",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "Sticky bot reasoning the brief depends on",
                },
                "source-match-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "Authoritative API reasoning",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 1
    assert payload["slugs_dropped"] == 1
    # No notes merge because the sticky winner is immutable.
    assert payload["notes_merged"] == 0

    state = _read_state(state_path)
    # Sticky entry wins over the source-host-match.
    assert "sticky-2026" in state
    assert "source-match-2026" not in state
    # Sticky entry's bot_notes preserved verbatim.
    assert state["sticky-2026"]["bot_notes"] == "Sticky bot reasoning the brief depends on"
    assert state["sticky-2026"]["shown_in_brief"] is True
    assert state["sticky-2026"]["last_shown_date"] == "2026-05-10"


def test_user_actioned_beats_sticky_on_priority(dedup_by_url, tmp_path, capsys):
    """When both `user_actioned: true` AND `shown_in_brief: true`
    entries collide on one URL, `user_actioned` wins (priority a > b).
    The user's verdict beats stickiness."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "sticky-2026": {
                    "status": "open",
                    "shown_in_brief": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "sticky notes",
                },
                "user-actioned-2026": {
                    "status": "sent",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "user submitted on 2026-04-01",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    assert "user-actioned-2026" in state
    assert "sticky-2026" not in state


def test_sticky_winner_with_source_match_in_sticky_cohort(dedup_by_url, tmp_path, capsys):
    """Two sticky entries collide — within the sticky cohort the
    source-host-match rule still runs so the authoritative API row
    survives. Without the cohort-internal recursion, alphabetical
    would pick the wrong sticky entry."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "alpha-sticky-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "shown_in_brief": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "alpha note",
                },
                "zulu-sticky-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "shown_in_brief": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "authoritative API note",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    # zulu-sticky wins because its source matches the URL host,
    # even though alpha-sticky is alphabetically earlier.
    assert "zulu-sticky-2026" in state
    assert "alpha-sticky-2026" not in state


def test_multiple_user_actioned_on_same_url_skipped(dedup_by_url, tmp_path, capsys):
    """Two `user_actioned: true` entries on one normalised URL is
    operator territory — the script refuses to pick a winner and
    leaves the group untouched with a stderr warning. Counter goes
    into skipped_multi_user_actioned."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "conf-a-2026": {
                    "status": "sent",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
                "conf-b-2026": {
                    "status": "dismissed",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 0
    assert payload["slugs_dropped"] == 0
    assert payload["skipped_multi_user_actioned"] == 1
    assert "multiple user_actioned" in captured.err

    state = _read_state(state_path)
    # Both entries still present.
    assert "conf-a-2026" in state
    assert "conf-b-2026" in state


def test_no_source_match_falls_back_to_alphabetical_winner(dedup_by_url, tmp_path, capsys):
    """No `user_actioned`, no source-host match → the
    alphabetically-earliest slug wins. Deterministic so the same
    backfill produces the same result on every run."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "zzz-conf-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/conf-2026",
                    "bot_notes": "from zzz",
                },
                "aaa-conf-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/conf-2026",
                    "bot_notes": "from aaa",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    # "aaa..." wins alphabetically.
    assert "aaa-conf-2026" in state
    assert "zzz-conf-2026" not in state
    # zzz's notes merged into aaa with provenance marker.
    winner_notes = state["aaa-conf-2026"]["bot_notes"]
    assert "from aaa" in winner_notes
    assert "[merged from zzz-conf-2026]" in winner_notes
    assert "from zzz" in winner_notes


def test_loser_empty_notes_not_merged(dedup_by_url, tmp_path, capsys):
    """When the loser has no `bot_notes` (empty or missing), no merge
    happens — but the loser is still dropped and the group counts as
    merged. `notes_merged` stays 0 because no notes were actually
    moved."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "Keeper's reasoning",
                },
                "loser-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    # No bot_notes field at all.
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["groups_merged"] == 1
    assert payload["slugs_dropped"] == 1
    assert payload["notes_merged"] == 0
    state = _read_state(state_path)
    # Winner's bot_notes unchanged (no provenance marker appended).
    assert state["winner-2026"]["bot_notes"] == "Keeper's reasoning"


def test_loser_notes_already_substring_not_merged(dedup_by_url, tmp_path, capsys):
    """If the loser's bot_notes is already a substring of the winner's,
    the merge is a no-op (idempotency). The loser is still dropped."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "Keeper's reasoning; common phrase",
                },
                "loser-2026": {
                    "status": "open",
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "common phrase",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    # No provenance marker appended (notes already contained).
    assert state["winner-2026"]["bot_notes"] == "Keeper's reasoning; common phrase"


def test_idempotent_second_run_is_noop(dedup_by_url, tmp_path, capsys):
    """First run resolves the collision; second run sees no collisions
    and is a true no-op (no write — mtime unchanged)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "A",
                },
                "loser-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "B",
                },
            }
        ),
        encoding="utf-8",
    )

    rc1 = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()
    assert rc1 == 0
    mtime_after_first = state_path.stat().st_mtime_ns

    rc2 = dedup_by_url.main(["--state-path", str(state_path)])
    captured2 = capsys.readouterr()
    assert rc2 == 0
    payload2 = _stdout_payload(captured2)
    assert payload2["groups_merged"] == 0
    assert payload2["slugs_dropped"] == 0
    # Second run did not write (mtime unchanged).
    assert state_path.stat().st_mtime_ns == mtime_after_first


def test_three_way_collision(dedup_by_url, tmp_path, capsys):
    """Three slugs share a URL — exactly one becomes the winner, two
    become losers, both get dropped, both notes merged."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "keeper",
                },
                "loser-a-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "alpha note",
                },
                "loser-b-2026": {
                    "source": "javaconferences.org",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "bravo note",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()
    assert rc == 0

    state = _read_state(state_path)
    assert "winner-2026" in state
    assert "loser-a-2026" not in state
    assert "loser-b-2026" not in state
    notes = state["winner-2026"]["bot_notes"]
    assert "keeper" in notes
    assert "[merged from loser-a-2026]: alpha note" in notes
    assert "[merged from loser-b-2026]: bravo note" in notes


def test_normalises_trailing_slash_and_case(dedup_by_url, tmp_path, capsys):
    """Two URLs that differ only in case + trailing slash dedup as
    one group — the normaliser rules out cosmetic differences before
    the collision check."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "conf-a-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://SESSIONIZE.COM/conf-2026/",
                    "bot_notes": "case+slash variant",
                },
                "conf-b-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                    "bot_notes": "canonical variant",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()
    assert rc == 0

    state = _read_state(state_path)
    # Sessionize-source winner survives, regardless of which URL form
    # it was written with.
    assert "conf-a-2026" in state
    assert "conf-b-2026" not in state


def test_missing_cfp_url_skipped(dedup_by_url, tmp_path, capsys):
    """Entries with no `cfp_url` field can't be deduped — they're
    skipped (no normalised key, never enter a collision group)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "no-url-2026": {"status": "open"},
                "has-url-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    assert "no-url-2026" in state
    assert "has-url-2026" in state


def test_underscore_keys_skipped(dedup_by_url, tmp_path, capsys):
    """`_blocked_prefixes` and other config keys are not CFP records —
    even if their value looks like a dict with a `cfp_url`, they
    must be left alone."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["devopsdays"],
                "_meta": {"cfp_url": "https://sessionize.com/conf-2026"},
                "real-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    # _meta NOT considered a CFP record, so no collision detected.
    assert "_meta" in state
    assert "_blocked_prefixes" in state
    assert "real-2026" in state


def test_non_dict_entry_skipped(dedup_by_url, tmp_path, capsys):
    """A list / string where a CFP record should be is skipped
    silently (matches backfill-source's defense against schema drift)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "broken-2026": ["not", "a", "dict"],
                "real-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    state = _read_state(state_path)
    assert state["broken-2026"] == ["not", "a", "dict"]
    assert "real-2026" in state


def test_atomic_write_via_replace(dedup_by_url, tmp_path, capsys, monkeypatch):
    """Sibling temp file + os.replace, not plain write_text. Concurrent
    containers run on the same /workspace/group/ directory, so a
    plain write would race with check-cfps / morning-brief writes
    and silently drop one side."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
                "loser-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )
    pre_write = state_path.read_text(encoding="utf-8")

    captured_calls = []
    real_replace = dedup_by_url.os.replace

    def spying_replace(src, dst):
        src_path = Path(src)
        dst_path = Path(dst)
        captured_calls.append(
            {
                "src": src_path,
                "dst": dst_path,
                "src_content": src_path.read_text(encoding="utf-8"),
                "dst_at_replace": dst_path.read_text(encoding="utf-8"),
            }
        )
        return real_replace(src, dst)

    monkeypatch.setattr(dedup_by_url.os, "replace", spying_replace)

    rc = dedup_by_url.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert len(captured_calls) == 1
    call = captured_calls[0]

    # Target path on dst, sibling temp on src.
    assert call["dst"] == state_path
    assert call["src"].parent == state_path.parent
    assert call["src"] != state_path

    # At replace time the sibling already holds the new state while
    # the target still has the pre-write content — atomicity guarantee.
    assert "loser-2026" not in call["src_content"]
    assert call["dst_at_replace"] == pre_write

    # And after the call no orphan temp file remains.
    final = _read_state(state_path)
    assert "loser-2026" not in final
    siblings = [p for p in tmp_path.iterdir() if p.name != "cfp-state.json"]
    assert siblings == [], f"orphan temp files left behind: {siblings}"


# ---------------------------------------------------------------------------
# --lookup mode — candidate-key rewrite delegation
# ---------------------------------------------------------------------------


def _run_lookup(dedup_by_url, state_path, urls, monkeypatch, capsys):
    """Helper: feed candidate URLs to the script via stdin's `--lookup`
    mode and parse the JSON mapping back out."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO("\n".join(urls) + "\n"))
    rc = dedup_by_url.main(["--state-path", str(state_path), "--lookup"])
    captured = capsys.readouterr()
    return rc, json.loads(captured.out.strip())


def test_lookup_maps_candidate_to_existing_slug(dedup_by_url, tmp_path, monkeypatch, capsys):
    """A candidate URL that normalises to the same `<host><path>` as
    an existing state entry's `cfp_url` maps to that entry's slug.
    This is the headline #240 case: agent's candidate is keyed
    `codemotion-milan-2026` from developers.events, state has
    `codemotion-milan-26` from Sessionize, both point at the same
    URL — lookup returns the existing slug so the agent rewrites the
    candidate to update-existing rather than create-new."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "codemotion-milan-26": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/codemotion-milan-26",
                },
            }
        ),
        encoding="utf-8",
    )
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        ["https://sessionize.com/codemotion-milan-26"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert result == {"https://sessionize.com/codemotion-milan-26": "codemotion-milan-26"}


def test_lookup_returns_null_for_no_collision(dedup_by_url, tmp_path, monkeypatch, capsys):
    """A candidate whose URL doesn't normalise to any existing state
    entry's URL maps to `null` — the agent flows that candidate
    through as a new entry unchanged."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
            }
        ),
        encoding="utf-8",
    )
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        ["https://sessionize.com/codemotion-milan-26"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert result == {"https://sessionize.com/codemotion-milan-26": None}


def test_lookup_normalises_cosmetic_differences(dedup_by_url, tmp_path, monkeypatch, capsys):
    """Case, trailing slash, scheme, and query/fragment differences
    between the candidate URL and the state entry's URL must NOT
    block the match — the normaliser strips them all before
    comparing."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "real-2026": {
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        [
            "https://SESSIONIZE.COM/conf-2026/",
            "http://sessionize.com/conf-2026?utm=foo",
            "https://sessionize.com/conf-2026#section",
        ],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    for url in result:
        assert result[url] == "real-2026"


def test_lookup_multi_url_batch(dedup_by_url, tmp_path, monkeypatch, capsys):
    """A realistic Step-7 call: multiple candidate URLs in one stdin
    payload, mix of colliding and non-colliding. Output is a single
    JSON mapping covering all inputs."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "existing-a-2026": {
                    "cfp_url": "https://sessionize.com/conf-a-2026",
                },
                "existing-b-2026": {
                    "cfp_url": "https://developers.events/cfps/conf-b-2026/",
                },
            }
        ),
        encoding="utf-8",
    )
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        [
            "https://sessionize.com/conf-a-2026",
            "https://developers.events/cfps/conf-b-2026/",
            "https://sessionize.com/new-conf-2026",
        ],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert result == {
        "https://sessionize.com/conf-a-2026": "existing-a-2026",
        "https://developers.events/cfps/conf-b-2026/": "existing-b-2026",
        "https://sessionize.com/new-conf-2026": None,
    }


def test_lookup_missing_state_file_returns_all_null(dedup_by_url, tmp_path, monkeypatch, capsys):
    """Lookup against a missing state file is a documented no-op:
    every candidate maps to None (no state means no collisions).
    Matches the dedup-mode missing-file contract for symmetry."""
    state_path = tmp_path / "absent.json"
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        ["https://sessionize.com/conf-2026"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    assert result == {"https://sessionize.com/conf-2026": None}


def test_lookup_skips_underscore_and_non_dict(dedup_by_url, tmp_path, monkeypatch, capsys):
    """`_blocked_prefixes` and other config keys, plus any non-dict
    entries (schema-drift defense), must NOT participate in the
    lookup index — same skip rules as the dedup mode."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["x"],
                "_meta": {"cfp_url": "https://sessionize.com/conf-2026"},
                "broken-2026": ["not", "a", "dict"],
                "real-2026": {"cfp_url": "https://sessionize.com/conf-2026"},
            }
        ),
        encoding="utf-8",
    )
    rc, result = _run_lookup(
        dedup_by_url,
        state_path,
        ["https://sessionize.com/conf-2026"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    # _meta and broken-2026 are skipped; real-2026 wins.
    assert result == {"https://sessionize.com/conf-2026": "real-2026"}


def test_lookup_blank_lines_in_stdin_ignored(dedup_by_url, tmp_path, monkeypatch, capsys):
    """Blank lines and surrounding whitespace in stdin are stripped
    so a one-per-line printf with trailing newline doesn't generate
    a phantom empty-string lookup entry."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"real-2026": {"cfp_url": "https://sessionize.com/conf-2026"}}),
        encoding="utf-8",
    )
    import io

    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("\n  https://sessionize.com/conf-2026  \n\n\n"),
    )
    rc = dedup_by_url.main(["--state-path", str(state_path), "--lookup"])
    captured = capsys.readouterr()
    result = json.loads(captured.out.strip())
    assert rc == 0
    assert result == {"https://sessionize.com/conf-2026": "real-2026"}


def test_lookup_does_not_mutate_state_file(dedup_by_url, tmp_path, monkeypatch, capsys):
    """Lookup mode is read-only — the state file's mtime must not
    change. Confirms the script's branch separation between dedup
    (mutates state) and lookup (read-only)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "winner-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
                "loser-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://sessionize.com/conf-2026",
                },
            }
        ),
        encoding="utf-8",
    )
    mtime_before = state_path.stat().st_mtime_ns

    rc, _ = _run_lookup(
        dedup_by_url,
        state_path,
        ["https://sessionize.com/conf-2026"],
        monkeypatch,
        capsys,
    )
    assert rc == 0
    # Despite the in-state collision (which the dedup mode would
    # resolve), lookup leaves the file untouched.
    assert state_path.stat().st_mtime_ns == mtime_before
    # Both colliding state entries still on disk.
    state = _read_state(state_path)
    assert "winner-2026" in state
    assert "loser-2026" in state
