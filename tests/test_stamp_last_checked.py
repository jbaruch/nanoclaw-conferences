"""Unit tests for skills/check-cfps/scripts/stamp-last-checked.py.

Locks down the evidence-gated freshness-heartbeat contract:
  - #4: `_last_checked` is the freshness heartbeat (was frozen at 2026-04-22
    while records were days fresh, reading as a 7-week stall).
  - #8: the stamp is GATED on verification evidence. It advances `_last_checked`
    only when verify-sessionize.py left a `verify-evidence.json` for THIS run
    showing a live Sessionize call (or nothing needed verifying). A run with no
    live verification does NOT advance the heartbeat: it records a distinct
    `_last_checked_skipped` and exits 3, so it can't report a clean success.
  - Touches only the two `_`-prefixed config keys; every CFP record and other
    config key is preserved (non-ASCII included).
  - Missing / corrupt / non-dict-root state, and write failures, exit 1.
"""

import json
from datetime import datetime, timezone

import pytest

_FROZEN_NOW = datetime(2026, 6, 15, 11, 36, 13, tzinfo=timezone.utc)
_FROZEN_ISO = "2026-06-15T11:36:13Z"
_TODAY = "2026-06-15"


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


def _evidence_file(
    tmp_path,
    *,
    run_date=_TODAY,
    slugs_expected=1,
    live_call=True,
    resolved=None,
    verify_failed=None,
):
    """Write a verify-evidence.json and return its path.

    By default a clean live marker (one entry resolved). `resolved` overrides
    the verified-verdict count and `verify_failed` the failure count so a test
    can express a total outage (live_call true, resolved=0, all verify_failed)."""
    if resolved is None:
        resolved = 1 if live_call else 0
    if verify_failed is None:
        verify_failed = 0 if live_call else slugs_expected
    path = tmp_path / "verify-evidence.json"
    path.write_text(
        json.dumps(
            {
                "run_date": run_date,
                "slugs_expected": slugs_expected,
                "live_call": live_call,
                "verified": resolved,
                "dismissed": 0,
                "dropped": 0,
                "verify_failed": verify_failed,
            }
        ),
        encoding="utf-8",
    )
    return path


def _run(module, state_path, evidence_path):
    return module.main(["--state", str(state_path), "--evidence", str(evidence_path)])


# --- clean stamp (verification evidenced) ---------------------------------


def test_live_evidence_stamps_run_timestamp(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open", "name": "A"}}), encoding="utf-8")
    evidence = _evidence_file(tmp_path, live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"_last_checked": _FROZEN_ISO, "verification": "live"}
    assert _read(state_path)["_last_checked"] == _FROZEN_ISO


def test_nothing_to_verify_stamps_clean(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"_blocked_prefixes": ["spam"]}), encoding="utf-8")
    # slugs_expected=0: a real run with no Sessionize cohort to verify.
    evidence = _evidence_file(tmp_path, slugs_expected=0, live_call=False)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured)["verification"] == "none-required"
    written = _read(state_path)
    assert written["_last_checked"] == _FROZEN_ISO
    assert written["_blocked_prefixes"] == ["spam"]


def test_clean_stamp_clears_prior_skipped_marker(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"_last_checked_skipped": "2026-06-14T05:00:00Z", "a-2026": {"status": "open"}}),
        encoding="utf-8",
    )
    evidence = _evidence_file(tmp_path, live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
    capsys.readouterr()

    assert rc == 0
    written = _read(state_path)
    assert written["_last_checked"] == _FROZEN_ISO
    assert "_last_checked_skipped" not in written


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
    evidence = _evidence_file(tmp_path, live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
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
    evidence = _evidence_file(tmp_path, live_call=True)

    _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    stamped = _payload(captured)["_last_checked"]
    assert stamped == _FROZEN_ISO
    assert stamped.endswith("Z")


# --- gated refusal (no live verification) ---------------------------------


def test_absent_evidence_refuses_no_advance(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"_last_checked": "2026-06-10T11:36:13Z", "a-2026": {"status": "open"}}),
        encoding="utf-8",
    )
    missing = tmp_path / "verify-evidence.json"  # never created

    rc = _run(stamp_last_checked, state_path, missing)
    captured = capsys.readouterr()

    assert rc == 3
    written = _read(state_path)
    # Heartbeat untouched; distinct skipped marker recorded instead.
    assert written["_last_checked"] == "2026-06-10T11:36:13Z"
    assert written["_last_checked_skipped"] == _FROZEN_ISO
    assert _payload(captured)["verification"] == "skipped"
    assert "not advanced" in captured.err


def test_no_marker_shape_refuses(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")
    # slugs were expected but nothing resolved (the #7 fake-work shape).
    evidence = _evidence_file(tmp_path, slugs_expected=5, live_call=False)

    rc = _run(stamp_last_checked, state_path, evidence)
    capsys.readouterr()

    assert rc == 3
    written = _read(state_path)
    assert "_last_checked" not in written
    assert written["_last_checked_skipped"] == _FROZEN_ISO


def test_total_outage_all_verify_failed_refuses(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")
    # The live call happened (per-slug isolation) but every slug errored, so
    # nothing was resolved from live data — must not pass the gate.
    evidence = _evidence_file(
        tmp_path, slugs_expected=3, live_call=True, resolved=0, verify_failed=3
    )

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 3
    assert "no entry resolved" in _payload(captured)["reason"]
    assert "_last_checked" not in _read(state_path)


def test_stale_dated_evidence_refuses(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")
    # A live marker, but from a prior run (reused cache).
    evidence = _evidence_file(tmp_path, run_date="2026-06-10", live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 3
    assert "stale" in _payload(captured)["reason"]
    assert "_last_checked" not in _read(state_path)


# --- I/O failure modes (still exit 1) -------------------------------------


def test_missing_state_file_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    evidence = _evidence_file(tmp_path, live_call=True)
    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err
    assert str(state_path) in captured.err


def test_corrupt_json_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{ not valid", encoding="utf-8")
    evidence = _evidence_file(tmp_path, live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err


def test_non_dict_root_exits_1(stamp_last_checked, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    evidence = _evidence_file(tmp_path, live_call=True)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected a JSON object" in captured.err


def test_write_failure_exits_1(stamp_last_checked, monkeypatch, tmp_path, capsys):
    _freeze(stamp_last_checked, monkeypatch)
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")
    evidence = _evidence_file(tmp_path, live_call=True)

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(stamp_last_checked, "_atomic_write_json", _boom)

    rc = _run(stamp_last_checked, state_path, evidence)
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot write" in captured.err
    assert str(state_path) in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
