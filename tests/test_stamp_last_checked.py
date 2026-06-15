"""Unit tests for skills/check-cfps/scripts/stamp-last-checked.py.

Locks down the freshness-heartbeat contract (jbaruch/nanoclaw-conferences#4
— `_last_checked` frozen at 2026-04-22 while records were days fresh, which
read as a 7-week pipeline stall):
  - Sets top-level `_last_checked` to the run timestamp (UTC ISO-8601, `Z`).
  - Writes UNCONDITIONALLY: a pre-existing `_last_checked` is overwritten and
    a file with no CFP records still gets stamped (a run that changed nothing
    still checked).
  - Touches only `_last_checked` — every CFP record and other `_`-config key
    is preserved (semantic JSON equality; the file is re-serialized, so byte
    layout may differ), non-ASCII included.
  - Missing / corrupt / non-dict-root state, and write failures, exit 1 with
    a stderr diagnostic.
"""

import json
from datetime import datetime, timezone

import pytest

_FROZEN_NOW = datetime(2026, 6, 15, 11, 36, 13, tzinfo=timezone.utc)
_FROZEN_ISO = "2026-06-15T11:36:13Z"


def _freeze(module, monkeypatch, instant=_FROZEN_NOW):
    class FrozenDateTime(module.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)


def _read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


def test_stamps_run_timestamp(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"a-2026": {"status": "open", "name": "A"}}),
        encoding="utf-8",
    )

    rc = stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"_last_checked": _FROZEN_ISO}
    assert _read(state_path)["_last_checked"] == _FROZEN_ISO


def test_overwrites_stale_value_unconditionally(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_last_checked": "2026-04-22T03:00:00Z",
                "a-2026": {"status": "open", "name": "A"},
            }
        ),
        encoding="utf-8",
    )

    rc = stamp_last_checked.main(["--state", str(state_path)])
    capsys.readouterr()

    assert rc == 0
    assert _read(state_path)["_last_checked"] == _FROZEN_ISO


def test_stamps_even_with_no_records(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"_blocked_prefixes": ["spam"]}), encoding="utf-8")

    rc = stamp_last_checked.main(["--state", str(state_path)])
    capsys.readouterr()

    assert rc == 0
    written = _read(state_path)
    assert written["_last_checked"] == _FROZEN_ISO
    assert written["_blocked_prefixes"] == ["spam"]


def test_preserves_records_and_unicode(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    record = {
        "schema_version": 1,
        "status": "open",
        "name": "Конференция 🎤",
        "bot_notes": "⚠️ STALE DATA — проверить дедлайн",
        "matched_interests": ["ai"],
    }
    state_path.write_text(
        json.dumps({"_blocked_prefixes": ["spam"], "x-2026": record}),
        encoding="utf-8",
    )

    rc = stamp_last_checked.main(["--state", str(state_path)])
    capsys.readouterr()

    assert rc == 0
    written = _read(state_path)
    assert written["_last_checked"] == _FROZEN_ISO
    assert written["x-2026"] == record
    assert written["_blocked_prefixes"] == ["spam"]


def test_output_timestamp_is_utc_iso_with_z(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({}), encoding="utf-8")

    stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    stamped = _payload(captured)["_last_checked"]
    assert stamped == _FROZEN_ISO
    assert stamped.endswith("Z")


def test_missing_state_file_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    rc = stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err
    assert str(state_path) in captured.err


def test_corrupt_json_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{ not valid", encoding="utf-8")

    rc = stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err


def test_non_dict_root_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected a JSON object" in captured.err


def test_write_failure_exits_1(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(stamp_last_checked, "_atomic_write_json", _boom)

    rc = stamp_last_checked.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot write" in captured.err
    assert str(state_path) in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
