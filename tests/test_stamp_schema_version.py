"""Unit tests for skills/check-cfps/scripts/stamp-schema-version.py.

Locks down the deterministic owner-migration contract (#308, 2026-05-31
incident — LLM hand-stamping left 0 of 642 records stamped, blanking the
brief):
  - Stamps `schema_version: 1` on every record dict that lacks it / has a
    different value; reports {total, stamped}.
  - Idempotent: a file already fully stamped reports stamped=0 and is NOT
    rewritten (mtime unchanged).
  - Skips `_`-prefixed config keys and non-dict values.
  - Preserves all other fields and non-ASCII content.
  - Missing / unreadable / non-dict-root state exits 1 with a stderr
    diagnostic (a failed owner migration must surface).
"""

import json

import pytest


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


def test_stamps_records_missing_version(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["spam"],
                "a-2026": {"status": "open", "name": "A"},
                "b-2026": {"status": "dismissed", "name": "B"},
            }
        ),
        encoding="utf-8",
    )

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"total": 2, "stamped": 2}
    written = _read(state_path)
    assert written["a-2026"]["schema_version"] == 1
    assert written["b-2026"]["schema_version"] == 1
    # `_`-prefixed config key is untouched.
    assert written["_blocked_prefixes"] == ["spam"]


def test_idempotent_no_rewrite_when_all_stamped(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"a-2026": {"status": "open", "name": "A", "schema_version": 1}}),
        encoding="utf-8",
    )
    before_mtime = state_path.stat().st_mtime_ns

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"total": 1, "stamped": 0}
    # Nothing changed → file not rewritten (mtime stable).
    assert state_path.stat().st_mtime_ns == before_mtime


def test_skips_non_dict_values(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "bad-2026": ["accidentally", "a", "list"],
                "good-2026": {"status": "open", "name": "Good"},
            }
        ),
        encoding="utf-8",
    )

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"total": 1, "stamped": 1}
    written = _read(state_path)
    assert written["good-2026"]["schema_version"] == 1
    # Non-dict value left as-is.
    assert written["bad-2026"] == ["accidentally", "a", "list"]


def test_preserves_other_fields_and_unicode(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    record = {
        "status": "open",
        "name": "Конференция 🎤",
        "bot_notes": "⚠️ STALE DATA — проверить дедлайн",
        "matched_interests": ["ai"],
    }
    state_path.write_text(json.dumps({"x-2026": record}), encoding="utf-8")

    rc = stamp_schema_version.main(["--state", str(state_path)])
    capsys.readouterr()

    assert rc == 0
    written = _read(state_path)
    assert written["x-2026"]["schema_version"] == 1
    assert written["x-2026"]["name"] == "Конференция 🎤"
    assert written["x-2026"]["bot_notes"] == "⚠️ STALE DATA — проверить дедлайн"
    assert written["x-2026"]["matched_interests"] == ["ai"]


def test_only_unstamped_records_counted(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "already-2026": {"status": "open", "schema_version": 1},
                "stale-2026": {"status": "open", "schema_version": 0},
                "missing-2026": {"status": "sent"},
            }
        ),
        encoding="utf-8",
    )

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"total": 3, "stamped": 2}
    written = _read(state_path)
    assert all(
        written[k]["schema_version"] == 1 for k in ("already-2026", "stale-2026", "missing-2026")
    )


def test_missing_state_file_exits_1(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err
    assert str(state_path) in captured.err


def test_corrupt_json_exits_1(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{ not valid", encoding="utf-8")

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err


def test_non_dict_root_exits_1(stamp_schema_version, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected a JSON object" in captured.err


def test_invalid_utf8_exits_1(stamp_schema_version, tmp_path, capsys):
    """A state file that is not valid UTF-8 gets the same exit-1 stderr
    diagnostic as malformed JSON, not an unhandled UnicodeDecodeError."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_bytes(b"\xff\xfe{}")

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err


def test_write_failure_exits_1(stamp_schema_version, tmp_path, capsys, monkeypatch):
    """When stamping needs a rewrite but the write fails, the script emits
    the documented stderr diagnostic and exits 1 instead of escaping with
    an OSError traceback. The failure is injected by monkeypatching the
    writer — directory-permission tricks are execution-identity dependent
    (root ignores permission bits), so they cannot fail deterministically."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"conf-2026": {"status": "open"}}), encoding="utf-8")

    def _boom(path, payload):
        raise OSError("boom")

    monkeypatch.setattr(stamp_schema_version, "_atomic_write_json", _boom)

    rc = stamp_schema_version.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot write" in captured.err
    assert str(state_path) in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
