"""Unit tests for skills/check-cfps/scripts/run-state.py.

Locks down the resumable-pipeline checkpoint store
(jbaruch/nanoclaw-conferences#4 — a token-limit continuation lost the
working set and reconstructed it from a chat summary):
  - `begin` starts fresh (resume:false) or resumes today's run (resume:true).
  - `save`/`load` round-trip a stage artifact; `completed` is order-preserving
    and deduped.
  - `begin` resets across a UTC-day boundary and on a corrupt manifest.
  - `load` of an unsaved stage exits 2; `done` tears the directory down.
  - Bad stage names / non-JSON artifacts exit 1 with a stderr diagnostic.
"""

import io
import json
from datetime import datetime, timezone

import pytest


def _freeze(module, monkeypatch, instant):
    class FrozenDateTime(module.datetime):
        @classmethod
        def now(cls, tz=None):
            return instant if tz is not None else instant.replace(tzinfo=None)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)


def _stdin(monkeypatch, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _out(capsys):
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def _manifest(run_dir):
    return json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))


def test_begin_fresh_creates_manifest(run_state, capsys):
    module, run_dir = run_state
    rc = module.main(["begin"])
    out = _out(capsys)

    assert rc == 0
    assert out["resume"] is False
    assert out["completed"] == []
    manifest = _manifest(run_dir)
    assert manifest["schema_version"] == 1
    assert manifest["completed"] == []
    assert manifest["run_date"] == out["run_date"]


def test_save_then_load_roundtrip(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()

    artifact = {"slugs": ["devnexus-2026"], "counts": {"unique_slugs": 1}}
    _stdin(monkeypatch, artifact)
    rc = module.main(["save", "prep"])
    assert rc == 0
    assert _out(capsys) == {"saved": "prep"}
    assert _manifest(run_dir)["completed"] == ["prep"]

    rc = module.main(["load", "prep"])
    assert rc == 0
    assert json.loads(capsys.readouterr().out) == artifact


def test_completed_is_ordered_and_deduped(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()

    for stage in ["fetch", "candidates", "prep", "fetch"]:
        _stdin(monkeypatch, {"stage": stage})
        module.main(["save", stage])
        capsys.readouterr()

    assert _manifest(run_dir)["completed"] == ["fetch", "candidates", "prep"]


def test_begin_resumes_same_day(run_state, monkeypatch, capsys):
    module, _ = run_state
    day = datetime(2026, 6, 15, 8, 0, 0, tzinfo=timezone.utc)
    _freeze(module, monkeypatch, day)

    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "fetch"])
    capsys.readouterr()

    # A continuation later the same day re-opens the store.
    _freeze(module, monkeypatch, day.replace(hour=23))
    rc = module.main(["begin"])
    out = _out(capsys)

    assert rc == 0
    assert out["resume"] is True
    assert out["completed"] == ["fetch"]
    # The earlier artifact survives the resume.
    module.main(["load", "fetch"])
    assert json.loads(capsys.readouterr().out) == {"a": 1}


def test_begin_resets_across_day_boundary(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    _freeze(module, monkeypatch, datetime(2026, 6, 15, 23, 0, 0, tzinfo=timezone.utc))
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "fetch"])
    capsys.readouterr()

    # Next UTC day → fresh run, stale artifacts cleared.
    _freeze(module, monkeypatch, datetime(2026, 6, 16, 1, 0, 0, tzinfo=timezone.utc))
    rc = module.main(["begin"])
    out = _out(capsys)

    assert rc == 0
    assert out["resume"] is False
    assert out["completed"] == []
    assert out["run_date"] == "2026-06-16"
    assert not (run_dir / "fetch.json").exists()
    assert module.main(["load", "fetch"]) == 2
    capsys.readouterr()


def test_begin_resets_on_corrupt_manifest(run_state, capsys):
    module, run_dir = run_state
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "manifest.json").write_text("{ not valid", encoding="utf-8")

    rc = module.main(["begin"])
    out = _out(capsys)

    assert rc == 0
    assert out["resume"] is False
    assert _manifest(run_dir)["completed"] == []


def test_begin_resets_on_unsupported_schema_version(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    day = datetime(2026, 6, 15, 9, 0, 0, tzinfo=timezone.utc)
    _freeze(module, monkeypatch, day)
    run_dir.mkdir(parents=True, exist_ok=True)
    # A future shape on today's date must NOT resume — the reader does not
    # migrate; it treats an unsupported version as no usable prior run.
    (run_dir / "manifest.json").write_text(
        json.dumps({"schema_version": 999, "run_date": "2026-06-15", "completed": ["fetch"]}),
        encoding="utf-8",
    )

    rc = module.main(["begin"])
    out = _out(capsys)

    assert rc == 0
    assert out["resume"] is False
    assert out["completed"] == []
    assert _manifest(run_dir)["schema_version"] == module.SCHEMA_VERSION


def test_load_absent_stage_exits_2(run_state, capsys):
    module, _ = run_state
    module.main(["begin"])
    capsys.readouterr()

    rc = module.main(["load", "prep"])
    captured = capsys.readouterr()

    assert rc == 2
    assert "no saved artifact for stage 'prep'" in captured.err


def test_done_clears_directory(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "fetch"])
    capsys.readouterr()

    rc = module.main(["done"])
    assert rc == 0
    assert _out(capsys) == {"cleared": True}
    assert not run_dir.exists()


def test_save_before_begin_still_persists(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    _stdin(monkeypatch, {"a": 1})

    rc = module.main(["save", "fetch"])
    assert rc == 0
    assert _out(capsys) == {"saved": "fetch"}
    assert (run_dir / "fetch.json").exists()
    assert _manifest(run_dir)["completed"] == ["fetch"]


def test_invalid_stage_name_exits_1(run_state, monkeypatch, capsys):
    module, _ = run_state
    module.main(["begin"])
    capsys.readouterr()

    _stdin(monkeypatch, {"a": 1})
    rc = module.main(["save", "Bad Stage"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid stage name" in captured.err

    rc = module.main(["load", "../escape"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid stage name" in captured.err


def test_save_non_json_exits_1(run_state, monkeypatch, capsys):
    module, _ = run_state
    module.main(["begin"])
    capsys.readouterr()

    monkeypatch.setattr("sys.stdin", io.StringIO("{ not valid"))
    rc = module.main(["save", "prep"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "not valid JSON" in captured.err


def test_invalidate_removes_stages_and_resume_rewinds(run_state, monkeypatch, capsys):
    """`invalidate verify working_set` after a failed verification gate:
    the artifacts are gone, the manifest keeps earlier stages, and a
    same-day `begin` resumes without the invalidated stages — so the
    retry re-runs live verification instead of reloading failed output."""
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    for stage in ("fetch", "candidates", "verify", "working_set"):
        _stdin(monkeypatch, {"stage": stage})
        module.main(["save", stage])
        capsys.readouterr()

    rc = module.main(["invalidate", "verify", "working_set"])
    assert rc == 0
    assert _out(capsys) == {"invalidated": ["verify", "working_set"], "absent": []}
    assert not (run_dir / "verify.json").exists()
    assert not (run_dir / "working_set.json").exists()
    assert (run_dir / "fetch.json").exists()
    assert _manifest(run_dir)["completed"] == ["fetch", "candidates"]

    rc = module.main(["begin"])
    out = _out(capsys)
    assert rc == 0
    assert out["resume"] is True
    assert out["completed"] == ["fetch", "candidates"]


def test_invalidate_absent_stage_is_idempotent(run_state, monkeypatch, capsys):
    module, _ = run_state
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "verify"])
    capsys.readouterr()

    module.main(["invalidate", "verify", "working_set"])
    capsys.readouterr()
    rc = module.main(["invalidate", "verify", "working_set"])
    assert rc == 0
    assert _out(capsys) == {"invalidated": [], "absent": ["verify", "working_set"]}


def test_invalidate_cascades_to_downstream_stages(run_state, monkeypatch, capsys):
    """Invalidating a mid-pipeline stage drops everything completed after
    it too: `completed` is completion-ordered, later stages derive from
    earlier ones, and resume treats `completed` as a prefix — a hole in
    the middle would let a stale downstream artifact pass as current."""
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    for stage in ("fetch", "candidates", "verify", "working_set"):
        _stdin(monkeypatch, {"stage": stage})
        module.main(["save", stage])
        capsys.readouterr()

    rc = module.main(["invalidate", "candidates"])
    assert rc == 0
    assert _out(capsys) == {
        "invalidated": ["candidates", "verify", "working_set"],
        "absent": [],
    }
    assert _manifest(run_dir)["completed"] == ["fetch"]
    assert (run_dir / "fetch.json").exists()
    assert not (run_dir / "verify.json").exists()


def test_invalidate_skips_invalid_cascaded_manifest_entries(run_state, monkeypatch, capsys):
    """Cascaded names come from manifest.completed, which is data, not
    trusted input — a tampered entry like "../escape" is skipped, never
    joined into an unlink path outside run_dir."""
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "verify"])
    capsys.readouterr()

    outside = run_dir.parent / "escape.json"
    outside.write_text("{}", encoding="utf-8")
    manifest = _manifest(run_dir)
    # Unhashable garbage BEFORE the target exercises the membership gate
    # (a raw `s in set(...)` would TypeError); "../escape" after it
    # exercises the traversal gate on cascaded names.
    manifest["completed"] = [["bad"], "verify", "../escape"]
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    rc = module.main(["invalidate", "verify"])
    assert rc == 0
    assert _out(capsys) == {"invalidated": ["verify"], "absent": []}
    assert outside.exists()
    assert _manifest(run_dir)["completed"] == [["bad"]]


def test_invalidate_manifest_write_failure_keeps_artifacts(run_state, monkeypatch, capsys):
    """The manifest rewrite happens before any unlink — if it fails, the
    command exits 1 with the artifacts intact, never a manifest that
    lists stages whose files are already gone."""
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "verify"])
    capsys.readouterr()

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_atomic_write_json", _boom)
    rc = module.main(["invalidate", "verify"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "invalidate failed" in captured.err
    assert (run_dir / "verify.json").exists()


def test_invalidate_covers_non_manifest_markers(run_state, capsys):
    """`verify-evidence.json` is written by the Step 5 driver, not via
    `save`, so it never appears in manifest.completed — invalidate still
    removes it so a retry cannot re-read stale failed evidence."""
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    (run_dir / "verify-evidence.json").write_text(
        json.dumps({"run_date": "2026-04-30", "live_resolved": 0}), encoding="utf-8"
    )

    rc = module.main(["invalidate", "verify-evidence"])
    assert rc == 0
    assert _out(capsys) == {"invalidated": ["verify-evidence"], "absent": []}
    assert not (run_dir / "verify-evidence.json").exists()


def test_invalidate_invalid_stage_name_exits_1(run_state, monkeypatch, capsys):
    module, run_dir = run_state
    module.main(["begin"])
    capsys.readouterr()
    _stdin(monkeypatch, {"a": 1})
    module.main(["save", "verify"])
    capsys.readouterr()

    rc = module.main(["invalidate", "verify", "../escape"])
    captured = capsys.readouterr()
    assert rc == 1
    assert "invalid stage name" in captured.err
    # Nothing was removed — all names validate before any unlink.
    assert (run_dir / "verify.json").exists()
    assert _manifest(run_dir)["completed"] == ["verify"]


def test_write_failure_exits_1(run_state, monkeypatch, capsys):
    module, _ = run_state

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(module, "_atomic_write_json", _boom)

    rc = module.main(["begin"])
    captured = capsys.readouterr()

    assert rc == 1
    assert "begin failed" in captured.err
    assert "disk full" in captured.err


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
