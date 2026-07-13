"""Unit tests for skills/check-cfps/scripts/commit-state.py.

Locks down the lock-owning Step 8 committer's contract
(jbaruch/nanoclaw-conferences#35 — the agent never writes
cfp-state.json directly):
  - The working set applies as per-slug replacements against a fresh
    locked read: untouched slugs survive, including one committed by a
    concurrent writer while this commit waited on the lock.
  - `user_actioned: true` ON DISK wins at commit time — the agent's
    copy may predate a mid-run user action.
  - `_`-prefixed payload keys are refused (config/heartbeat keys have
    their own writers), non-dict records are refused, malformed stdin
    is refused — all exit 1 with a diagnostic, state untouched.
  - Absent state file = first run; corrupt state = exit 1 diagnostic;
    contended lock honors the exit-1 contract.
"""

import io
import json
import threading

import pytest


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _read(state_path):
    return json.loads(state_path.read_text(encoding="utf-8"))


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_applies_per_slug_replacements(commit_state, tmp_path, monkeypatch, capsys):
    """New slugs are added, named slugs are replaced, everything else on
    disk (records and `_`-config alike) survives untouched."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["BlockMe"],
                "keep-conf-2026": {"status": "sent"},
                "update-conf-2026": {"status": "open", "bot_notes": "old"},
            }
        ),
        encoding="utf-8",
    )
    _stdin(
        monkeypatch,
        {
            "update-conf-2026": {"status": "open", "bot_notes": "new"},
            "new-conf-2026": {"status": "open"},
        },
    )

    rc = commit_state.main(["--state", str(state_path)])
    assert rc == 0
    assert _out(capsys) == {"written": 2, "skipped_user_actioned": 0, "total_records": 3}
    final = _read(state_path)
    assert final["keep-conf-2026"] == {"status": "sent"}
    assert final["update-conf-2026"]["bot_notes"] == "new"
    assert final["new-conf-2026"] == {"status": "open"}
    assert final["_blocked_prefixes"] == ["BlockMe"]


def test_user_actioned_on_disk_wins(commit_state, tmp_path, monkeypatch, capsys):
    """A record flagged user_actioned on the FRESH read is never
    overwritten, even when the payload carries a different copy."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"acted-conf-2026": {"status": "sent", "user_actioned": True}}),
        encoding="utf-8",
    )
    _stdin(monkeypatch, {"acted-conf-2026": {"status": "open"}})

    rc = commit_state.main(["--state", str(state_path)])
    assert rc == 0
    assert _out(capsys) == {"written": 0, "skipped_user_actioned": 1, "total_records": 1}
    assert _read(state_path)["acted-conf-2026"] == {"status": "sent", "user_actioned": True}


def test_underscore_key_in_payload_refused(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"a-2026": {"status": "open"}}), encoding="utf-8")
    _stdin(monkeypatch, {"_last_checked": "2026-07-08", "b-2026": {"status": "open"}})

    rc = commit_state.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "refusing to write config/heartbeat key" in captured.err
    assert _read(state_path) == {"a-2026": {"status": "open"}}


def test_non_dict_record_refused(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{}", encoding="utf-8")
    _stdin(monkeypatch, {"bad-2026": ["not", "a", "record"]})

    rc = commit_state.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected an object" in captured.err


def test_malformed_stdin_exits_1(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))

    rc = commit_state.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "stdin is not valid JSON" in captured.err


def test_absent_state_file_is_first_run(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    _stdin(monkeypatch, {"first-conf-2026": {"status": "open"}})

    rc = commit_state.main(["--state", str(state_path)])
    assert rc == 0
    assert _out(capsys) == {"written": 1, "skipped_user_actioned": 0, "total_records": 1}
    assert _read(state_path) == {"first-conf-2026": {"status": "open"}}


def test_corrupt_state_exits_1(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not json", encoding="utf-8")
    _stdin(monkeypatch, {"a-2026": {"status": "open"}})

    rc = commit_state.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "cannot read" in captured.err


def test_lock_timeout_exits_1(commit_state, state_lock, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CFP_STATE_LOCK_TIMEOUT", "0")
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{}", encoding="utf-8")
    _stdin(monkeypatch, {"a-2026": {"status": "open"}})

    with state_lock.locked(state_path, timeout=5.0):
        rc = commit_state.main(["--state", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "could not acquire" in captured.err


def test_concurrent_writer_update_survives_commit(commit_state, state_lock, tmp_path):
    """The lost-update regression for the main Step 8 writer: while the
    commit blocks on the lock, another writer lands a record — the
    commit's fresh read picks it up and both updates survive. No sleeps;
    flock is the synchronization."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"mine-2026": {"status": "open"}}), encoding="utf-8")

    def _run_commit():
        # Thread-local stdin: main() reads sys.stdin, so feed the payload
        # through a real pipe-free StringIO bound before the call.
        import sys

        old_stdin = sys.stdin
        sys.stdin = io.StringIO(json.dumps({"mine-2026": {"status": "sent"}}))
        try:
            commit_state.main(["--state", str(state_path)])
        finally:
            sys.stdin = old_stdin

    thread = threading.Thread(target=_run_commit)
    with state_lock.locked(state_path):
        thread.start()
        current = _read(state_path)
        current["theirs-2026"] = {"status": "open"}
        state_path.write_text(json.dumps(current), encoding="utf-8")
    thread.join(timeout=30)
    assert not thread.is_alive(), "commit-state never acquired the lock"

    final = _read(state_path)
    assert final["mine-2026"] == {"status": "sent"}
    assert final["theirs-2026"] == {"status": "open"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
