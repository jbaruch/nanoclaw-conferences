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
  - A first run whose working set is empty still materializes `{}` so
    the Step 8 stampers that follow have a file to read
    (jbaruch/nanoclaw-conferences#79); an existing file with nothing to
    write stays byte-identical.
"""

import io
import json
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CURSOR_SCRIPT = REPO_ROOT / "skills/nightly-cfp-sync/scripts/stamp-cursor.py"

# Fixed instants for the first-run chain (coding-policy: testing-standards —
# injected reference, never the wall clock). The wrapper run starts at
# RUN_START; stamp-last-checked's frozen clock lands the heartbeat at
# FROZEN_ISO, which is at/after RUN_START, so the cursor gate passes.
RUN_START = "2026-05-02T06:30:00Z"
FROZEN_NOW = datetime(2026, 5, 2, 6, 32, 0, tzinfo=UTC)
FROZEN_ISO = "2026-05-02T06:32:00Z"
CURSOR_NOW = "2026-05-02T06:33:00Z"


def _freeze(module, monkeypatch):
    """Pin `module.datetime.now()` at FROZEN_NOW — the same frozen-subclass
    idiom test_stamp_last_checked.py uses for the heartbeat stamp."""
    real = module.datetime

    class FrozenDateTime(real):
        @classmethod
        def now(cls, tz=None):
            return FROZEN_NOW if tz is not None else FROZEN_NOW.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)


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


def test_first_run_empty_commit_materializes_empty_state(
    commit_state, tmp_path, monkeypatch, capsys
):
    """Absent state file + empty working set: the file is created holding an
    empty object. Discovery finding nothing on a fresh install must not leave
    the stampers a missing file (jbaruch/nanoclaw-conferences#79)."""
    state_path = tmp_path / "cfp-state.json"
    _stdin(monkeypatch, {})

    rc = commit_state.main(["--state", str(state_path)])

    assert rc == 0
    assert _out(capsys) == {"written": 0, "skipped_user_actioned": 0, "total_records": 0}
    assert state_path.exists()
    assert _read(state_path) == {}


def test_existing_state_untouched_by_empty_commit(commit_state, tmp_path, monkeypatch, capsys):
    """The first-run materialization is scoped to an absent file: an existing
    state with nothing to write is left byte-identical."""
    state_path = tmp_path / "cfp-state.json"
    original = json.dumps({"a-2026": {"status": "open"}}, indent=4)
    state_path.write_text(original, encoding="utf-8")
    _stdin(monkeypatch, {})

    rc = commit_state.main(["--state", str(state_path)])

    assert rc == 0
    assert _out(capsys) == {"written": 0, "skipped_user_actioned": 0, "total_records": 1}
    assert state_path.read_text(encoding="utf-8") == original


def test_first_run_empty_commit_feeds_stampers_and_cursor(
    commit_state, stamp_schema_version, stamp_last_checked, tmp_path, monkeypatch, capsys
):
    """The whole first-run-with-no-candidates path: empty commit, then the
    schema stamper, the none-required heartbeat, and the wrapper cursor all
    succeed on the materialized state instead of exiting 1 on a missing file."""
    state_path = tmp_path / "cfp-state.json"
    _stdin(monkeypatch, {})
    assert commit_state.main(["--state", str(state_path)]) == 0
    capsys.readouterr()

    assert stamp_schema_version.main(["--state", str(state_path)]) == 0
    assert json.loads(capsys.readouterr().out.strip()) == {"total": 0, "stamped": 0}

    # Nothing was discovered, so there is no Sessionize cohort to verify:
    # sessionize_total == 0 is the heartbeat's "none-required" branch.
    evidence = tmp_path / "verify-evidence.json"
    evidence.write_text(
        json.dumps(
            {
                "run_date": FROZEN_NOW.date().isoformat(),
                "slugs_expected": 0,
                "sessionize_total": 0,
                "live_call": False,
                "verified": 0,
                "dismissed": 0,
                "dropped": 0,
                "verify_failed": 0,
            }
        ),
        encoding="utf-8",
    )
    _freeze(stamp_last_checked, monkeypatch)
    rc = stamp_last_checked.main(["--state", str(state_path), "--evidence", str(evidence)])
    assert rc == 0
    assert json.loads(capsys.readouterr().out.strip()) == {
        "_last_checked": FROZEN_ISO,
        "verification": "none-required",
    }

    cursor_path = tmp_path / "nightly-cfp-sync-cursor.json"
    proc = subprocess.run(
        [
            "python3",
            str(CURSOR_SCRIPT),
            "--cursor",
            str(cursor_path),
            "--state",
            str(state_path),
            "--since",
            RUN_START,
            "--now",
            CURSOR_NOW,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip())["status"] == "stamped"
    assert _read(cursor_path)["last_run"] == CURSOR_NOW


def test_guarded_commit_preserves_changes_to_the_same_slug(
    commit_state, tmp_path, monkeypatch, capsys
):
    state_path = tmp_path / "cfp-state.json"
    original = {"status": "open", "bot_notes": "unjudged"}
    fresh = {"status": "approved", "bot_notes": "newer model judgment", "shown_in_brief": True}
    state_path.write_text(json.dumps({"same": fresh, "other": {"status": "sent"}}))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"same": commit_state.record_digest(original)}))
    _stdin(monkeypatch, {"same": {"status": "dismissed", "bot_notes": "stale result"}})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 0
    assert _out(capsys)["skipped_changed"] == ["same"]
    assert _read(state_path) == {"same": fresh, "other": {"status": "sent"}}


def test_guarded_commit_writes_only_matching_records(commit_state, tmp_path, monkeypatch, capsys):
    state_path = tmp_path / "cfp-state.json"
    source = {"status": "open", "name": "A"}
    state_path.write_text(
        json.dumps({"matched": source, "owner": {"status": "sent", "user_actioned": True}})
    )
    expected = tmp_path / "expected.json"
    expected.write_text(
        json.dumps(
            {
                "matched": commit_state.record_digest(source),
                "owner": commit_state.record_digest(source),
                "new": commit_state.record_digest(None),
            }
        )
    )
    _stdin(
        monkeypatch,
        {
            "matched": {"status": "dismissed"},
            "owner": {"status": "dismissed"},
            "new": {"status": "open"},
        },
    )
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 0
    result = _out(capsys)
    assert result == {
        "written": 2,
        "skipped_user_actioned": 1,
        "total_records": 3,
        "skipped_changed": [],
    }
    assert _read(state_path)["owner"] == {"status": "sent", "user_actioned": True}


@pytest.mark.parametrize("bad", [{}, {"a": "wrong"}, {"a": "0" * 64, "extra": "1" * 64}, []])
def test_guarded_commit_refuses_incomplete_fingerprints(
    commit_state, tmp_path, monkeypatch, capsys, bad
):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text('{"a":{"status":"open"}}')
    before = state_path.read_bytes()
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps(bad))
    _stdin(monkeypatch, {"a": {"status": "dismissed"}})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 1
    assert state_path.read_bytes() == before


def test_guarded_commit_compares_after_obtaining_lock(
    commit_state, state_lock, tmp_path, monkeypatch, capsys
):
    state_path = tmp_path / "cfp-state.json"
    original = {"status": "open"}
    changed = {"status": "approved", "bot_notes": "changed while commit waited"}
    state_path.write_text(json.dumps({"same": original}))
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"same": commit_state.record_digest(original)}))
    _stdin(monkeypatch, {"same": {"status": "dismissed"}})
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(
            commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)])
        )
    )
    with state_lock.locked(state_path):
        thread.start()
        state_path.write_text(json.dumps({"same": changed}))
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert outcomes == [0]
    assert _out(capsys)["skipped_changed"] == ["same"]
    assert _read(state_path)["same"] == changed


@pytest.mark.parametrize("content", [None, "not json"])
def test_guarded_commit_requires_a_readable_snapshot(
    commit_state, tmp_path, monkeypatch, capsys, content
):
    state_path = tmp_path / "cfp-state.json"
    original = '{"a":{"status":"open"}}'
    state_path.write_text(original)
    expected = tmp_path / "expected.json"
    if content is not None:
        expected.write_text(content)
    _stdin(monkeypatch, {"a": {"status": "dismissed"}})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 1
    assert "restore the snapshot" in capsys.readouterr().err
    assert state_path.read_text() == original


def test_guarded_commit_refuses_nonfinite_stored_source(
    commit_state, tmp_path, monkeypatch, capsys
):
    state_path = tmp_path / "cfp-state.json"
    original = '{"a":{"status":"open","score":NaN}}'
    state_path.write_text(original)
    expected = tmp_path / "expected.json"
    expected.write_text(json.dumps({"a": "0" * 64}))
    _stdin(monkeypatch, {"a": {"status": "dismissed"}})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 1
    assert "repair the state" in capsys.readouterr().err
    assert state_path.read_text() == original


def test_snapshot_cli_canonicalizes_sources_without_opening_state(
    commit_state, tmp_path, monkeypatch, capsys
):
    state_path = tmp_path / "missing-parent" / "cfp-state.json"
    record = {"name": "Café", "status": "open"}
    _stdin(monkeypatch, {"same": record, "new": None})
    assert commit_state.main(["--state", str(state_path), "--snapshot"]) == 0
    first = _out(capsys)
    _stdin(monkeypatch, {"new": None, "same": dict(reversed(list(record.items())))})
    assert commit_state.main(["--state", str(state_path), "--snapshot"]) == 0
    assert _out(capsys) == first
    assert set(first) == {"same", "new"}
    assert all(len(value) == 64 for value in first.values())
    assert first["same"] != first["new"]
    assert not state_path.parent.exists()


@pytest.mark.parametrize(
    "payload", [{"_last_checked": {}}, {"a": []}, {"a": {"score": float("nan")}}]
)
def test_snapshot_refuses_unusable_sources(commit_state, tmp_path, monkeypatch, capsys, payload):
    state_path = tmp_path / "state.json"
    _stdin(monkeypatch, payload)
    assert commit_state.main(["--state", str(state_path), "--snapshot"]) == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err
    assert not state_path.exists()


@pytest.mark.parametrize("acted", [False, True])
def test_unique_reference_guard_preserves_existing_owner_and_other_records(
    commit_state, tmp_path, monkeypatch, capsys, acted
):
    state_path, expected = tmp_path / "cfp.json", tmp_path / "expected.json"
    prior = {"status": "open", "cfp_url": "https://example.invalid/cfp", "user_actioned": acted}
    state = {"one": prior, "unrelated": {"keep": "fresh"}, "_last_checked": "preserved"}
    state_path.write_text(json.dumps(state))
    expected.write_text(json.dumps({"one": commit_state.record_digest(prior)}))
    _stdin(monkeypatch, {"one": {**prior, "status": "sent"}})
    assert (
        commit_state.main(
            [
                "--state",
                str(state_path),
                "--expected-digests",
                str(expected),
                "--unique-cfp-url",
                "https://example.invalid/cfp/",
            ]
        )
        == 0
    )
    result = _out(capsys)
    assert result["written"] == (0 if acted else 1)
    assert result["skipped_user_actioned"] == (1 if acted else 0)
    if not acted:
        state["one"]["status"] = "sent"
    assert _read(state_path) == state


def test_unique_reference_guard_compares_siblings_after_obtaining_actual_lock(
    commit_state, state_lock, tmp_path, monkeypatch, capsys
):
    state_path, expected = tmp_path / "cfp.json", tmp_path / "expected.json"
    prior = {"status": "open", "cfp_url": "https://example.invalid/cfp"}
    state_path.write_text(json.dumps({"one": prior}))
    expected.write_text(json.dumps({"one": commit_state.record_digest(prior)}))
    _stdin(monkeypatch, {"one": {**prior, "status": "sent"}})
    outcomes = []
    thread = threading.Thread(
        target=lambda: outcomes.append(
            commit_state.main(
                [
                    "--state",
                    str(state_path),
                    "--expected-digests",
                    str(expected),
                    "--unique-cfp-url",
                    "https://example.invalid/cfp",
                ]
            )
        )
    )
    state = {"one": prior, "new-sibling": {**prior, "cfp_url": "https://example.invalid/cfp/"}}
    with state_lock.locked(state_path):
        thread.start()
        state_path.write_text(json.dumps(state))
    thread.join(timeout=30)
    assert not thread.is_alive() and outcomes == [0]
    result = _out(capsys)
    assert result["written"] == 0 and result["skipped_changed"] == ["one"]
    assert _read(state_path) == state


@pytest.mark.parametrize("mode", ["unguarded", "snapshot", "wrong-record", "multiple"])
def test_unique_reference_guard_cannot_grant_a_new_commit_mode(
    commit_state, tmp_path, monkeypatch, capsys, mode
):
    state_path, expected = tmp_path / "cfp.json", tmp_path / "expected.json"
    prior = {"status": "open", "cfp_url": "https://example.invalid/cfp"}
    state_path.write_text(json.dumps({"one": prior}))
    before = state_path.read_bytes()
    working = {"one": {**prior, "status": "sent"}}
    args = ["--state", str(state_path), "--unique-cfp-url", "https://example.invalid/cfp"]
    if mode == "snapshot":
        args.append("--snapshot")
    elif mode != "unguarded":
        args += ["--expected-digests", str(expected)]
        expected.write_text(json.dumps({"one": commit_state.record_digest(prior)}))
        if mode == "wrong-record":
            working["one"]["cfp_url"] = "https://example.invalid/other"
        else:
            working["two"] = dict(prior)
    _stdin(monkeypatch, working)
    assert commit_state.main(args) == 1
    assert capsys.readouterr().err and state_path.read_bytes() == before


def test_existing_digest_only_empty_bootstrap_contract_is_unchanged(
    commit_state, tmp_path, monkeypatch, capsys
):
    state_path, expected = tmp_path / "cfp.json", tmp_path / "expected.json"
    expected.write_text("{}")
    _stdin(monkeypatch, {})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 0
    assert _read(state_path) == {}
    assert _out(capsys) == {
        "written": 0,
        "skipped_user_actioned": 0,
        "total_records": 0,
        "skipped_changed": [],
    }


def test_guarded_new_record_feeds_owner_schema_stamper(
    commit_state, stamp_schema_version, tmp_path, monkeypatch, capsys
):
    state_path = tmp_path / "cfp-state.json"
    expected = tmp_path / "expected.json"
    _stdin(monkeypatch, {"new": None})
    assert commit_state.main(["--snapshot"]) == 0
    expected.write_text(json.dumps(_out(capsys)))
    _stdin(monkeypatch, {"new": {"status": "open"}})
    assert commit_state.main(["--state", str(state_path), "--expected-digests", str(expected)]) == 0
    assert _out(capsys)["written"] == 1
    assert stamp_schema_version.main(["--state", str(state_path)]) == 0
    assert _out(capsys) == {"total": 1, "stamped": 1}
    assert _read(state_path)["new"] == {"status": "open", "schema_version": 1}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
