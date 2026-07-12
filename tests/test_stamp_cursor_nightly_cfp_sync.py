"""Tests for skills/nightly-cfp-sync/scripts/stamp-cursor.py."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/nightly-cfp-sync/scripts/stamp-cursor.py"

# Frozen clock for the subprocess tests (coding-policy: testing-standards —
# inject a fixed reference instant, never the wall clock). NOW_UTC and the
# same-UTC-date FRESH heartbeat below drive the evidence gate deterministically.
NOW_UTC = "2026-05-02T08:00:00Z"
FRESH_LAST_CHECKED = "2026-05-02T07:59:00Z"  # same UTC date as NOW_UTC -> gate passes
STALE_LAST_CHECKED = "2026-05-01T23:59:00Z"  # day before -> gate refuses


def _write_state(path: Path, last_checked: str | None) -> Path:
    """Write a minimal cfp-state.json with the given top-level `_last_checked`
    (omitted when None). Returns the path for use as `--state`."""
    state: dict = {"cfps": []}
    if last_checked is not None:
        state["_last_checked"] = last_checked
    path.write_text(json.dumps(state), encoding="utf-8")
    return path


@pytest.fixture
def stamp_module():
    spec = importlib.util.spec_from_file_location(
        "stamp_cursor_nightly_cfp_sync_under_test", REPO_ROOT / SCRIPT_REL
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load {SCRIPT_REL}: confirm SCRIPT_REL still points at the "
            "checked-in script (update it if the script was renamed/moved) and rerun pytest"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stamp_writes_supported_schema_and_utc_iso(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    payload = stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 14, 7, tzinfo=timezone.utc))
    assert payload["status"] == "stamped"
    assert payload["last_run"] == "2026-05-02T03:14:07Z"
    on_disk = json.loads(cursor.read_text())
    assert on_disk["schema_version"] == stamp_module.SUPPORTED_SCHEMA


def test_stamp_creates_parent_dirs(stamp_module, tmp_path):
    cursor = tmp_path / "nested" / "cursor.json"
    stamp_module.stamp(cursor, datetime(2026, 5, 2, tzinfo=timezone.utc))
    assert cursor.exists()


def test_stamp_overwrites_existing_cursor(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-23T01:00:00Z"}))
    stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc))
    assert json.loads(cursor.read_text())["last_run"] == "2026-05-02T03:00:00Z"


def test_stamp_leaves_no_tempfile_debris(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    stamp_module.stamp(cursor, datetime(2026, 5, 2, tzinfo=timezone.utc))
    assert list(tmp_path.glob(".cursor.json.*.tmp")) == []


def test_stamp_preserves_existing_file_mode(stamp_module, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-23T00:00:00Z"}))
    os.chmod(cursor, 0o600)
    stamp_module.stamp(cursor, datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc))
    assert stat.S_IMODE(os.stat(cursor).st_mode) == 0o600


def test_main_emits_status_stamped_and_exits_zero(tmp_path):
    cursor = tmp_path / "cursor.json"
    state = _write_state(tmp_path / "cfp-state.json", FRESH_LAST_CHECKED)
    env = {**os.environ, "NIGHTLY_CFP_SYNC_CURSOR": str(cursor)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL), "--state", str(state), "--now", NOW_UTC],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["status"] == "stamped"


def test_main_cli_flag_overrides_env_var(tmp_path):
    env_cursor = tmp_path / "from-env.json"
    flag_cursor = tmp_path / "from-flag.json"
    state = _write_state(tmp_path / "cfp-state.json", FRESH_LAST_CHECKED)
    env = {**os.environ, "NIGHTLY_CFP_SYNC_CURSOR": str(env_cursor)}
    proc = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / SCRIPT_REL),
            "--cursor",
            str(flag_cursor),
            "--state",
            str(state),
            "--now",
            NOW_UTC,
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    assert flag_cursor.exists()
    assert not env_cursor.exists()


def test_main_exits_2_on_write_failure(tmp_path):
    # A fresh heartbeat passes the gate so the run reaches the cursor write,
    # which fails on the unwritable directory -> exit 2 (distinct from the
    # gated-refusal exit 3).
    state = _write_state(tmp_path / "cfp-state.json", FRESH_LAST_CHECKED)
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    os.chmod(locked_dir, 0o500)
    try:
        proc = subprocess.run(
            [
                "python3",
                str(REPO_ROOT / SCRIPT_REL),
                "--cursor",
                str(locked_dir / "cursor.json"),
                "--state",
                str(state),
                "--now",
                NOW_UTC,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert proc.returncode == 2
    finally:
        os.chmod(locked_dir, 0o700)


def test_heartbeat_fresh_when_last_checked_is_today(stamp_module, tmp_path):
    state = _write_state(tmp_path / "cfp-state.json", "2026-05-02T01:23:45Z")
    fresh, reason = stamp_module.heartbeat_is_fresh(state, "2026-05-02")
    assert fresh is True
    assert "fresh" in reason


def test_heartbeat_stale_when_last_checked_is_prior_day(stamp_module, tmp_path):
    state = _write_state(tmp_path / "cfp-state.json", STALE_LAST_CHECKED)
    fresh, reason = stamp_module.heartbeat_is_fresh(state, "2026-05-02")
    assert fresh is False
    assert "stale" in reason


def test_heartbeat_not_fresh_when_last_checked_absent(stamp_module, tmp_path):
    state = _write_state(tmp_path / "cfp-state.json", None)
    fresh, reason = stamp_module.heartbeat_is_fresh(state, "2026-05-02")
    assert fresh is False
    assert "no _last_checked" in reason


def test_heartbeat_not_fresh_when_state_missing(stamp_module, tmp_path):
    fresh, reason = stamp_module.heartbeat_is_fresh(tmp_path / "absent.json", "2026-05-02")
    assert fresh is False
    assert "cannot read" in reason


def test_heartbeat_not_fresh_when_last_checked_unparseable(stamp_module, tmp_path):
    state = _write_state(tmp_path / "cfp-state.json", "2026-05-02")  # date only, no time
    fresh, reason = stamp_module.heartbeat_is_fresh(state, "2026-05-02")
    assert fresh is False
    assert "parseable" in reason


def test_main_exits_3_and_leaves_cursor_untouched_on_stale_heartbeat(tmp_path):
    cursor = tmp_path / "cursor.json"
    state = _write_state(tmp_path / "cfp-state.json", STALE_LAST_CHECKED)
    proc = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / SCRIPT_REL),
            "--cursor",
            str(cursor),
            "--state",
            str(state),
            "--now",
            NOW_UTC,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 3
    payload = json.loads(proc.stdout)
    assert payload["status"] == "skipped"
    assert payload["verification"] == "unevidenced"
    assert not cursor.exists()  # cursor must not advance on a gated refusal


def test_main_exits_3_when_state_missing(tmp_path):
    cursor = tmp_path / "cursor.json"
    proc = subprocess.run(
        [
            "python3",
            str(REPO_ROOT / SCRIPT_REL),
            "--cursor",
            str(cursor),
            "--state",
            str(tmp_path / "absent.json"),
            "--now",
            NOW_UTC,
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert proc.returncode == 3
    assert not cursor.exists()
