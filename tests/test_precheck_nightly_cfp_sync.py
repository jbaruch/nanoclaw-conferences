"""Tests for skills/nightly-cfp-sync/scripts/precheck-nightly-cfp-sync.py."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/nightly-cfp-sync/scripts/precheck-nightly-cfp-sync.py"


@pytest.fixture
def precheck():
    spec = importlib.util.spec_from_file_location(
        "precheck_nightly_cfp_sync_under_test", REPO_ROOT / SCRIPT_REL
    )
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load {SCRIPT_REL}: confirm SCRIPT_REL still points at the "
            "checked-in script (update it if the script was renamed/moved) and rerun pytest"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_decide_wakes_when_cursor_absent(precheck, tmp_path):
    cursor = tmp_path / "missing.json"
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "no_cursor"


def test_decide_no_wake_within_cadence(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-05-01T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is False
    assert result["data"]["reason"] == "within_cadence"


def test_decide_wakes_when_cadence_elapsed(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-28T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cadence_elapsed"


def test_decide_wakes_at_three_day_boundary(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-04-29T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cadence_elapsed"


def test_decide_wakes_on_future_timestamp(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2030-01-01T00:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_future"


def test_decide_wakes_on_unparseable_iso(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "garbage"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_unparseable"


def test_decide_wakes_on_unsupported_schema(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 99, "last_run": "2026-05-01T03:00:00Z"}))
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_error"


def test_decide_wakes_on_malformed_json(precheck, tmp_path):
    cursor = tmp_path / "cursor.json"
    cursor.write_text("{not valid")
    now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
    result = precheck.decide(now, cursor)
    assert result["wake_agent"] is True
    assert result["data"]["reason"] == "cursor_error"


def test_decide_cursor_permission_denied_fails_open(precheck, tmp_path):
    locked_dir = tmp_path / "locked"
    locked_dir.mkdir()
    cursor = locked_dir / "cursor.json"
    cursor.write_text(json.dumps({"schema_version": 1, "last_run": "2026-05-01T03:00:00Z"}))
    os.chmod(locked_dir, 0o000)
    try:
        now = datetime(2026, 5, 2, 3, 0, tzinfo=timezone.utc)
        result = precheck.decide(now, cursor)
        assert result["wake_agent"] is True
        assert result["data"]["reason"] == "cursor_error"
    finally:
        os.chmod(locked_dir, 0o700)


def test_main_emits_json_and_exits_zero_on_no_cursor(tmp_path):
    cursor = tmp_path / "cursor.json"
    env = {**os.environ, "NIGHTLY_CFP_SYNC_CURSOR": str(cursor)}
    proc = subprocess.run(
        ["python3", str(REPO_ROOT / SCRIPT_REL)],
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )
    assert proc.returncode == 0
    payload = json.loads(proc.stdout)
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "no_cursor"


def test_main_fails_open_on_unexpected_error(precheck, monkeypatch, capsys):
    """outer-boundary-process-contract: an unexpected exception inside the
    decision path must not crash the precheck (which the scheduler reads as
    'do not wake'). main() catches it at the boundary, emits a fail-open
    wake payload, and still exits 0."""

    def _boom(*_a, **_k):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(precheck, "decide", _boom)
    code = precheck.main()
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["wake_agent"] is True
    assert payload["data"]["reason"] == "precheck_error"
    assert "RuntimeError: unexpected" in payload["data"]["error"]
