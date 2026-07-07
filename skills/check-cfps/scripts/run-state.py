#!/usr/bin/env python3
"""Run-state checkpoint store for the check-cfps pipeline (resumable runs).

check-cfps is an agent-orchestrated pipeline: deterministic helper scripts
(fetch, prepare-sessionize-batch, apply-sessionize-results, dedup, ...)
bracket agent-only steps (MCP calls, web search, relevance judgment). The
agent historically held every stage's intermediate artifact in context and
persisted only at Step 8, so a token-limit continuation lost the working
set and reconstructed it from a chat summary
(jbaruch/nanoclaw-conferences#4 — the 2026-06-10 run blew its budget
mid-pipeline, then re-derived prep output and the state schema from memory).

This script gives each stage a durable, machine-readable checkpoint on
disk so a continuation re-reads the last artifact instead of rebuilding it.
Artifacts live in a per-run directory (default
`/workspace/group/state/cfp-run/`, override via `CFP_RUN_STATE_DIR`):

  manifest.json   {"schema_version": 1, "run_date": "<YYYY-MM-DD UTC>",
                   "completed": ["fetch", "candidates", ...]}
  <stage>.json    the JSON artifact saved for that stage

Subcommands:
  begin          Start (or resume) a run. If manifest.json exists with
                 run_date == today (UTC), resume: emit
                 {"resume": true, "run_date", "completed": [...]}. Otherwise
                 (absent, stale date, or unreadable manifest) reset the dir
                 to a fresh manifest and emit
                 {"resume": false, "run_date", "completed": []}.
  save <stage>   Read a JSON artifact on stdin, write <stage>.json
                 atomically, append <stage> to manifest.completed
                 (order-preserving, deduped). Emit {"saved": "<stage>"}.
  load <stage>   Print the saved <stage>.json artifact verbatim. Exit 2 if
                 no artifact was saved for that stage.
  done           Remove the run directory (success teardown). Emit
                 {"cleared": true}.
  invalidate <stage>...
                 Remove the named stages' artifacts and drop them from
                 manifest.completed, so a same-day resume re-runs those
                 steps instead of reloading failed output. Also accepts
                 non-manifest markers in the run dir (verify-evidence).
                 Idempotent — absent stages are reported, not errors.
                 Emit {"invalidated": [...], "absent": [...]}.

Resume is best-effort, not a correctness requirement: stages are
idempotent and Step 5 re-verifies the full cohort, so a fresh full run is
always safe. `begin` resets across UTC-day boundaries precisely so a
days-later continuation starts clean rather than resuming a stale run.

Stage names are free-form lowercase identifiers (`[a-z0-9][a-z0-9_-]*`);
check-cfps uses fetch, candidates, verify, working_set. Schema doc:
`references/run-state.md`.

Exit 0 on success; exit 1 with a stderr diagnostic on bad usage /
malformed input / I/O failure; exit 2 when `load` is asked for an absent
stage.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RUN_DIR = Path("/workspace/group/state/cfp-run")
MANIFEST_NAME = "manifest.json"
SCHEMA_VERSION = 1
STAGE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _run_dir() -> Path:
    override = os.environ.get("CFP_RUN_STATE_DIR")
    return Path(override) if override else DEFAULT_RUN_DIR


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _atomic_write_json(path: Path, payload) -> None:
    """Write `payload` as JSON to `path` via temp file + fsync + os.replace,
    preserving the existing file's mode (0644 fallback). Raises on failure;
    cleanup uses try/finally (no broad except) per
    `coding-policy: error-handling`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    )
    replaced = False
    try:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, path)
        replaced = True
    finally:
        if not replaced:
            if not tmp.closed:
                tmp.close()
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass


def _read_manifest(run_dir: Path):
    """Return the manifest dict, or None when absent/unreadable/not a dict
    (any of which `begin` treats as 'no usable prior run')."""
    try:
        data = json.loads((run_dir / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _clear_dir(run_dir: Path) -> None:
    """Delete the run dir's files (manifest + saved stage artifacts).
    Only files are removed — no recursion — since the store is flat."""
    if not run_dir.exists():
        return
    for child in run_dir.iterdir():
        if child.is_file():
            child.unlink()


def cmd_begin(run_dir: Path) -> int:
    today = _today()
    manifest = _read_manifest(run_dir)
    if (
        manifest is not None
        and manifest.get("schema_version") == SCHEMA_VERSION
        and manifest.get("run_date") == today
        and isinstance(manifest.get("completed"), list)
    ):
        print(json.dumps({"resume": True, "run_date": today, "completed": manifest["completed"]}))
        return 0

    _clear_dir(run_dir)
    fresh = {"schema_version": SCHEMA_VERSION, "run_date": today, "completed": []}
    _atomic_write_json(run_dir / MANIFEST_NAME, fresh)
    print(json.dumps({"resume": False, "run_date": today, "completed": []}))
    return 0


def cmd_save(run_dir: Path, stage: str) -> int:
    if not STAGE_RE.match(stage):
        sys.stderr.write(f"run-state: invalid stage name {stage!r}\n")
        return 1
    raw = sys.stdin.read()
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"run-state: stage {stage!r} stdin is not valid JSON: {exc}\n")
        return 1

    manifest = _read_manifest(run_dir)
    if manifest is None:
        # save before begin (or after a corrupt manifest): start a minimal
        # run rather than dropping the artifact on the floor. No reset here —
        # never destroy artifacts on a save.
        manifest = {"schema_version": SCHEMA_VERSION, "run_date": _today(), "completed": []}

    _atomic_write_json(run_dir / f"{stage}.json", artifact)

    completed = manifest.get("completed")
    if not isinstance(completed, list):
        completed = []
    if stage not in completed:
        completed.append(stage)
    manifest["completed"] = completed
    manifest.setdefault("schema_version", SCHEMA_VERSION)
    manifest.setdefault("run_date", _today())
    _atomic_write_json(run_dir / MANIFEST_NAME, manifest)

    print(json.dumps({"saved": stage}))
    return 0


def cmd_load(run_dir: Path, stage: str) -> int:
    if not STAGE_RE.match(stage):
        sys.stderr.write(f"run-state: invalid stage name {stage!r}\n")
        return 1
    path = run_dir / f"{stage}.json"
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        sys.stderr.write(f"run-state: no saved artifact for stage {stage!r}\n")
        return 2
    except (OSError, UnicodeDecodeError) as exc:
        sys.stderr.write(f"run-state: cannot read stage {stage!r}: {type(exc).__name__}: {exc}\n")
        return 1
    try:
        json.loads(text)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"run-state: saved stage {stage!r} is corrupt JSON: {exc}\n")
        return 1
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_invalidate(run_dir: Path, stages: list) -> int:
    """Remove the named stages so a same-day resume re-runs them. Used on
    verification-gate failure (stamp-last-checked exit 3): keeping `verify`
    and `working_set` checkpointed would let the retry reload the same
    failed evidence and repeat the refusal without a new Sessionize call
    (jbaruch/nanoclaw-conferences#31)."""
    for stage in stages:
        if not STAGE_RE.match(stage):
            sys.stderr.write(f"run-state: invalid stage name {stage!r}\n")
            return 1

    invalidated = []
    absent = []
    for stage in stages:
        try:
            (run_dir / f"{stage}.json").unlink()
            invalidated.append(stage)
        except FileNotFoundError:
            absent.append(stage)

    manifest = _read_manifest(run_dir)
    if manifest is not None:
        completed = manifest.get("completed")
        if isinstance(completed, list):
            remaining = [s for s in completed if s not in stages]
            if remaining != completed:
                manifest["completed"] = remaining
                _atomic_write_json(run_dir / MANIFEST_NAME, manifest)

    print(json.dumps({"invalidated": invalidated, "absent": absent}))
    return 0


def cmd_done(run_dir: Path) -> int:
    _clear_dir(run_dir)
    if run_dir.exists():
        try:
            run_dir.rmdir()
        except OSError:
            # Non-empty (a subdir we didn't create) — leave it; the files
            # are already gone, which is what `done` promises.
            pass
    print(json.dumps({"cleared": True}))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run-state checkpoint store for the check-cfps pipeline."
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("begin", help="start or resume a run")
    p_save = sub.add_parser("save", help="save a stage artifact from stdin")
    p_save.add_argument("stage")
    p_load = sub.add_parser("load", help="print a saved stage artifact")
    p_load.add_argument("stage")
    sub.add_parser("done", help="clear the run directory on success")
    p_inv = sub.add_parser("invalidate", help="remove stages so a resume re-runs them")
    p_inv.add_argument("stages", nargs="+")
    args = parser.parse_args(argv)

    run_dir = _run_dir()
    # Catch write/remove/mkdir failures from any subcommand so the process
    # contract is explicit (exit 1 + stderr diagnostic) rather than an
    # accidental traceback per `coding-policy: script-delegation`. cmd_load's
    # own read handling returns 2/1 before reaching here.
    try:
        if args.command == "begin":
            return cmd_begin(run_dir)
        if args.command == "save":
            return cmd_save(run_dir, args.stage)
        if args.command == "load":
            return cmd_load(run_dir, args.stage)
        if args.command == "invalidate":
            return cmd_invalidate(run_dir, args.stages)
        # `done` — the only remaining branch under a required subparser.
        return cmd_done(run_dir)
    except OSError as exc:
        sys.stderr.write(f"run-state: {args.command} failed: {type(exc).__name__}: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
