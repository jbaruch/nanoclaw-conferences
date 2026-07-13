"""Unit tests for skills/check-cfps/scripts/expire-cfps.py.

Locks down the script's documented contract (all runs pass an explicit
`--today` so nothing here reads the wall clock):

  - `open`/`approved` non-Sessionize entries with `deadline < today`
    → `status: "expired"` + idempotent bot_notes marker.
  - `deadline == today` is still open (end-of-deadline-day rule,
    matching the fetcher's `days_left < 0` drop).
  - `user_actioned: true` never touched (`skipped_user_actioned`).
  - Sessionize entries skipped — explicit `source` OR host-inferred
    from `cfp_url` when `source` is absent (`skipped_sessionize`).
  - Missing/unparseable `deadline` skipped (`skipped_no_deadline`).
  - Non-expirable statuses (dismissed, sent, remind, conflict,
    expired) untouched.
  - Expiry overrides `shown_in_brief` stickiness.
  - Idempotent: second run expires nothing and does not write.
  - Missing state file → no-op exit 0; corrupt JSON / non-dict root
    → exit 1 with stderr diagnostic.
  - Underscore-prefixed config keys and non-dict entries skipped.
  - Atomic write via the sibling temp-file + os.replace helper.
"""

import json

import pytest

TODAY = "2024-08-01"


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stdout_payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


def _run(expire_cfps, state_path):
    return expire_cfps.main(["--state-path", str(state_path), "--today", TODAY])


# ---------------------------------------------------------------------------
# parse_deadline / effective_source unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected_iso",
    [
        ("2024-07-04", "2024-07-04"),
        # First-10-chars slice tolerates a timestamp suffix.
        ("2024-07-04T12:00:00Z", "2024-07-04"),
        ("not a date", None),
        ("", None),
        (None, None),
        (20240704, None),
    ],
)
def test_parse_deadline(expire_cfps, value, expected_iso):
    result = expire_cfps.parse_deadline(value)
    assert (result.isoformat() if result else None) == expected_iso


def test_effective_source_prefers_explicit_over_host(expire_cfps):
    """An explicit `source` wins; host inference only fills the gap."""
    assert (
        expire_cfps.effective_source(
            {"source": "developers.events", "cfp_url": "https://sessionize.com/x"}
        )
        == "developers.events"
    )
    assert (
        expire_cfps.effective_source({"cfp_url": "https://sessionize.com/x"})
        == "sessionize-speaker-api"
    )
    assert expire_cfps.effective_source({"cfp_url": "https://example.com/x"}) is None


# ---------------------------------------------------------------------------
# main() scenarios
# ---------------------------------------------------------------------------


def test_live_repro_zombie_open_rows_expire(expire_cfps, tmp_path, capsys):
    """The issue #27 shape (dates as stable past dates): a
    developers.events `open` row whose deadline passed becomes
    `expired` with a bot_notes marker; a still-open row is untouched."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "zombie-conf-2024": {
                    "status": "open",
                    "name": "Zombie Conf",
                    "deadline": "2024-07-04",
                    "source": "developers.events",
                    "cfp_url": "https://zombie.example.com/cfp",
                    "bot_notes": "",
                },
                "alive-conf-2024": {
                    "status": "open",
                    "name": "Alive Conf",
                    "deadline": "2024-09-01",
                    "source": "developers.events",
                    "cfp_url": "https://alive.example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 1
    assert payload["expired_slugs"] == ["zombie-conf-2024"]

    state = _read_state(state_path)
    assert state["zombie-conf-2024"]["status"] == "expired"
    assert state["zombie-conf-2024"]["bot_notes"] == "Expired: CFP deadline 2024-07-04 passed."
    assert state["alive-conf-2024"]["status"] == "open"


def test_deadline_today_still_open(expire_cfps, tmp_path, capsys):
    """`deadline == today` is NOT expired — submissions close at the
    end of the deadline day, matching the fetcher's `days_left < 0`
    drop rule."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "today-conf-2024": {
                    "status": "open",
                    "deadline": TODAY,
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert _stdout_payload(captured)["expired"] == 0
    assert _read_state(state_path)["today-conf-2024"]["status"] == "open"


def test_approved_rows_also_expire(expire_cfps, tmp_path, capsys):
    """`approved` (e.g. javaconferences.org Tier-1) rows expire the
    same way as `open` ones."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "approved-conf-2024": {
                    "status": "approved",
                    "deadline": "2024-06-01",
                    "source": "javaconferences.org",
                    "cfp_url": "https://example.com/cfp",
                    "bot_notes": "Auto-approved: javaconferences.org source",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert _stdout_payload(captured)["expired"] == 1
    entry = _read_state(state_path)["approved-conf-2024"]
    assert entry["status"] == "expired"
    # Marker appended after the existing notes with the separator.
    assert entry["bot_notes"] == (
        "Auto-approved: javaconferences.org source | Expired: CFP deadline 2024-06-01 passed."
    )


def test_user_actioned_never_touched(expire_cfps, tmp_path, capsys):
    """Immutability invariant: a past-deadline `open` row with
    `user_actioned: true` is skipped and counted, not expired."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "user-conf-2024": {
                    "status": "open",
                    "user_actioned": True,
                    "deadline": "2024-01-01",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )
    mtime_before = state_path.stat().st_mtime_ns

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 0
    assert payload["skipped_user_actioned"] == 1
    assert _read_state(state_path)["user-conf-2024"]["status"] == "open"
    assert state_path.stat().st_mtime_ns == mtime_before


def test_sessionize_rows_skipped_explicit_and_inferred(expire_cfps, tmp_path, capsys):
    """Sessionize rows belong to the live-verify path: skipped when
    `source` says so explicitly AND when an unsourced row's `cfp_url`
    host infers it — a stored Sessionize deadline may be stale against
    the API, so expiring on it would kill a live CFP."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "explicit-sz-2024": {
                    "status": "open",
                    "deadline": "2024-01-01",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/explicit",
                },
                "inferred-sz-2024": {
                    "status": "open",
                    "deadline": "2024-01-01",
                    "cfp_url": "https://sessionize.com/inferred",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 0
    assert payload["skipped_sessionize"] == 2
    state = _read_state(state_path)
    assert state["explicit-sz-2024"]["status"] == "open"
    assert state["inferred-sz-2024"]["status"] == "open"


def test_unsourced_non_sessionize_row_expires(expire_cfps, tmp_path, capsys):
    """An unsourced row whose host infers to nothing known is
    deadline-of-record → expirable."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "unsourced-2024": {
                    "status": "open",
                    "deadline": "2024-01-01",
                    "cfp_url": "https://cfp.example-conf.io/2024",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert _stdout_payload(captured)["expired"] == 1
    assert _read_state(state_path)["unsourced-2024"]["status"] == "expired"


def test_missing_or_unparseable_deadline_skipped(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "no-deadline-2024": {
                    "status": "open",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/a",
                },
                "bad-deadline-2024": {
                    "status": "open",
                    "deadline": "sometime in spring",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/b",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 0
    assert payload["skipped_no_deadline"] == 2


def test_non_expirable_statuses_untouched(expire_cfps, tmp_path, capsys):
    """dismissed / sent / remind / conflict / already-expired rows are
    outside the pass entirely — no counter, no mutation."""
    state_path = tmp_path / "cfp-state.json"
    entries = {
        f"{status}-conf-2024": {
            "status": status,
            "deadline": "2024-01-01",
            "source": "developers.events",
            "cfp_url": f"https://example.com/{status}",
        }
        for status in ("dismissed", "sent", "remind", "conflict", "expired")
    }
    state_path.write_text(json.dumps(entries), encoding="utf-8")

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 0
    state = _read_state(state_path)
    for slug, entry in entries.items():
        assert state[slug]["status"] == entry["status"]


def test_expiry_overrides_sticky(expire_cfps, tmp_path, capsys):
    """`shown_in_brief: true` does not protect a past-deadline row —
    same basis as the Step 8 confirmed-closed stickiness exception."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "sticky-conf-2024": {
                    "status": "open",
                    "shown_in_brief": True,
                    "deadline": "2024-05-05",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                    "bot_notes": "sticky reasoning",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    assert _stdout_payload(captured)["expired"] == 1
    entry = _read_state(state_path)["sticky-conf-2024"]
    assert entry["status"] == "expired"
    assert entry["bot_notes"] == "sticky reasoning | Expired: CFP deadline 2024-05-05 passed."


def test_idempotent_second_run_is_noop(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "zombie-conf-2024": {
                    "status": "open",
                    "deadline": "2024-07-04",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    rc1 = _run(expire_cfps, state_path)
    _ = capsys.readouterr()
    assert rc1 == 0
    mtime_after_first = state_path.stat().st_mtime_ns
    notes_after_first = _read_state(state_path)["zombie-conf-2024"]["bot_notes"]

    rc2 = _run(expire_cfps, state_path)
    captured2 = capsys.readouterr()
    assert rc2 == 0
    payload2 = _stdout_payload(captured2)
    assert payload2["expired"] == 0
    assert state_path.stat().st_mtime_ns == mtime_after_first
    assert _read_state(state_path)["zombie-conf-2024"]["bot_notes"] == notes_after_first


def test_underscore_keys_and_non_dict_entries_skipped(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["devopsdays"],
                "_meta": {"status": "open", "deadline": "2024-01-01"},
                "broken-2024": ["not", "a", "dict"],
                "real-2024": {
                    "status": "open",
                    "deadline": "2024-01-01",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["expired"] == 1
    assert payload["expired_slugs"] == ["real-2024"]
    state = _read_state(state_path)
    assert state["_meta"] == {"status": "open", "deadline": "2024-01-01"}
    assert state["broken-2024"] == ["not", "a", "dict"]


def test_missing_state_file_is_no_op_exit_0(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload == {
        "expired": 0,
        "skipped_user_actioned": 0,
        "skipped_sessionize": 0,
        "skipped_no_deadline": 0,
        "expired_slugs": [],
    }
    assert "state file not found" in captured.err


def test_corrupt_json_exits_1(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_invalid_utf8_exits_1(expire_cfps, tmp_path, capsys):
    """A state file that is not valid UTF-8 gets the same exit-1 stderr
    diagnostic as malformed JSON, not an unhandled UnicodeDecodeError."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_bytes(b"\xff\xfe{}")

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_root_not_dict_exits_1(expire_cfps, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = _run(expire_cfps, state_path)
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected dict" in captured.err


def test_atomic_write_via_replace(expire_cfps, tmp_path, capsys, monkeypatch):
    """The write goes through the sibling temp file + os.replace path
    reused from dedup-by-url.py, and leaves no orphan temp file."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "zombie-conf-2024": {
                    "status": "open",
                    "deadline": "2024-07-04",
                    "source": "developers.events",
                    "cfp_url": "https://example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    # `_atomic_write` lives in the sibling dedup-by-url module and
    # calls `os.replace` through the shared `os` module object, so
    # patching it globally reaches the reused helper.
    import os as os_module

    replace_calls = []
    orig = os_module.replace

    def spying_replace(src, dst):
        replace_calls.append((src, dst))
        return orig(src, dst)

    monkeypatch.setattr(os_module, "replace", spying_replace)

    rc = _run(expire_cfps, state_path)
    _ = capsys.readouterr()

    assert rc == 0
    assert len(replace_calls) == 1
    assert _read_state(state_path)["zombie-conf-2024"]["status"] == "expired"
    siblings = [
        p
        for p in tmp_path.iterdir()
        # The advisory lock file is intentional and persistent (state_lock.py)
        # — only orphan .tmp files count as atomicity leaks.
        if p.name not in ("cfp-state.json", "cfp-state.json.lock")
    ]
    assert siblings == [], f"orphan temp files left behind: {siblings}"
