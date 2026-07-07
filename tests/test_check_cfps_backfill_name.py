"""Unit tests for skills/check-cfps/scripts/backfill-name.py.

Locks down the script's documented contract:

  - Name derivation: slug with one trailing `-20\\d\\d` year suffix
    stripped, hyphens split, words capitalized; slug that yields no
    words falls back to the normalised `cfp_url`; neither → entry
    left nameless and counted in `unnamed_remaining`.
  - Entries with a non-empty string `name` are untouched
    (`skipped_named`); non-string / whitespace-only names are treated
    as missing and repaired.
  - `user_actioned: true` entries are never mutated (immutability
    invariant) — nameless ones surface in `skipped_user_actioned`.
  - Underscore-prefixed config keys and non-dict entries skipped.
  - Idempotent: second run backfills nothing and does not write.
  - Missing state file → no-op exit 0 with zero counters.
  - Corrupt JSON / non-dict root → exit 1 with stderr diagnostic.
  - Atomic write via sibling temp file + os.replace (reused from
    dedup-by-url.py; concurrent containers share /workspace/group/).
"""

import json

import pytest


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stdout_payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# derive_name unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "slug,cfp_url,expected",
    [
        # The live #23 case: year suffix stripped, words capitalized.
        ("devoxx-morocco-2024", "https://dvma24.cfp.dev", "Devoxx Morocco"),
        ("vibe-coding-con-2024", None, "Vibe Coding Con"),
        # Only ONE trailing year suffix is stripped.
        ("conf-2023-2024", None, "Conf 2023"),
        # No year suffix → whole slug used.
        ("jfokus", None, "Jfokus"),
        # Slug that is nothing but a year → URL fallback.
        ("2024", "https://dvma24.cfp.dev/", "dvma24.cfp.dev"),
        # Neither slug words nor URL → None.
        ("2024", None, None),
        ("2024", "", None),
    ],
)
def test_derive_name(backfill_name, slug, cfp_url, expected):
    assert backfill_name.derive_name(slug, cfp_url) == expected


# ---------------------------------------------------------------------------
# main() scenarios
# ---------------------------------------------------------------------------


def test_live_repro_devoxx_morocco_stub(backfill_name, tmp_path, capsys):
    """The nameless stub shape from issue #23 (dates shifted to stable
    past dates per testing-standards — the script never reads the
    clock) gets a derived name and becomes visible to the priority
    matcher and the brief."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "devoxx-morocco-2024": {
                    "status": "open",
                    "deadline": "2024-07-04",
                    "city": "Casablanca (Morocco)",
                    "conf_date": "2024-11-03",
                    "cfp_url": "https://dvma24.cfp.dev",
                    "source": "developers.events",
                    "bot_notes": "",
                    "matched_interests": [],
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 1
    assert payload["unnamed_remaining"] == 0
    assert payload["named"] == {"devoxx-morocco-2024": "Devoxx Morocco"}

    state = _read_state(state_path)
    assert state["devoxx-morocco-2024"]["name"] == "Devoxx Morocco"
    # Everything else untouched.
    assert state["devoxx-morocco-2024"]["deadline"] == "2024-07-04"


def test_named_entries_untouched(backfill_name, tmp_path, capsys):
    """Entries that already carry a real name are skipped — no write
    happens when nothing was backfilled (mtime unchanged)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "devoxx-belgium-2026": {
                    "status": "open",
                    "name": "Devoxx Belgium",
                    "cfp_url": "https://dvbe26.cfp.dev",
                },
            }
        ),
        encoding="utf-8",
    )
    mtime_before = state_path.stat().st_mtime_ns

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 0
    assert payload["skipped_named"] == 1
    assert state_path.stat().st_mtime_ns == mtime_before


def test_whitespace_or_non_string_name_repaired(backfill_name, tmp_path, capsys):
    """A whitespace-only or non-string `name` is as useless as a
    missing one — both get repaired."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "blank-conf-2026": {
                    "name": "   ",
                    "cfp_url": "https://example.com/cfp",
                },
                "drifted-conf-2026": {
                    "name": ["not", "a", "string"],
                    "cfp_url": "https://example.com/cfp2",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 2

    state = _read_state(state_path)
    assert state["blank-conf-2026"]["name"] == "Blank Conf"
    assert state["drifted-conf-2026"]["name"] == "Drifted Conf"


def test_user_actioned_entries_never_mutated(backfill_name, tmp_path, capsys):
    """The immutability invariant from references/contracts.md: a
    nameless `user_actioned: true` record is NOT backfilled — the
    user's records are never bot-mutated. It is surfaced in the
    `skipped_user_actioned` counter instead, and no write happens
    when nothing else was backfilled."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "user-conf-2024": {
                    "status": "sent",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/user-conf",
                },
            }
        ),
        encoding="utf-8",
    )
    mtime_before = state_path.stat().st_mtime_ns

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 0
    assert payload["skipped_user_actioned"] == 1
    assert payload["named"] == {}

    state = _read_state(state_path)
    assert "name" not in state["user-conf-2024"]
    assert state_path.stat().st_mtime_ns == mtime_before


def test_unnamed_remaining_counted(backfill_name, tmp_path, capsys):
    """An entry whose slug is all-year and has no usable cfp_url stays
    nameless but is surfaced in the counter instead of rotting
    silently."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"2026": {"status": "open"}}),
        encoding="utf-8",
    )

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 0
    assert payload["unnamed_remaining"] == 1


def test_underscore_keys_and_non_dict_entries_skipped(backfill_name, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["devopsdays"],
                "_meta": {"cfp_url": "https://example.com"},
                "broken-2026": ["not", "a", "dict"],
                "real-2026": {"status": "open", "cfp_url": "https://example.com/cfp"},
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 1
    assert payload["named"] == {"real-2026": "Real"}

    state = _read_state(state_path)
    assert state["_blocked_prefixes"] == ["devopsdays"]
    assert "name" not in state["_meta"]
    assert state["broken-2026"] == ["not", "a", "dict"]


def test_idempotent_second_run_is_noop(backfill_name, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"some-conf-2026": {"status": "open"}}),
        encoding="utf-8",
    )

    rc1 = backfill_name.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()
    assert rc1 == 0
    mtime_after_first = state_path.stat().st_mtime_ns

    rc2 = backfill_name.main(["--state-path", str(state_path)])
    captured2 = capsys.readouterr()
    assert rc2 == 0
    payload2 = _stdout_payload(captured2)
    assert payload2["backfilled"] == 0
    assert payload2["skipped_named"] == 1
    assert state_path.stat().st_mtime_ns == mtime_after_first


def test_missing_state_file_is_no_op_exit_0(backfill_name, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload == {
        "backfilled": 0,
        "skipped_named": 0,
        "skipped_user_actioned": 0,
        "unnamed_remaining": 0,
        "named": {},
    }
    assert "state file not found" in captured.err


def test_corrupt_json_exits_1(backfill_name, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_invalid_utf8_exits_1(backfill_name, tmp_path, capsys):
    """A state file that is not valid UTF-8 gets the same exit-1 stderr
    diagnostic as malformed JSON, not an unhandled UnicodeDecodeError."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_bytes(b"\xff\xfe{}")

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_root_not_dict_exits_1(backfill_name, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = backfill_name.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected dict" in captured.err


def test_atomic_write_via_replace(backfill_name, tmp_path, capsys, monkeypatch):
    """The write goes through the sibling temp file + os.replace path
    reused from dedup-by-url.py, and leaves no orphan temp file."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"some-conf-2026": {"status": "open"}}),
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

    rc = backfill_name.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert len(replace_calls) == 1
    state = _read_state(state_path)
    assert state["some-conf-2026"]["name"] == "Some Conf"
    siblings = [p for p in tmp_path.iterdir() if p.name != "cfp-state.json"]
    assert siblings == [], f"orphan temp files left behind: {siblings}"
