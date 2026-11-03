#!/usr/bin/env python3
"""Same-day checkpoints for the check-cfps pipeline, so a continuation resumes.

On 2026-06-10 a run blew its token budget mid-pipeline and rebuilt its working
set from a chat summary (jbaruch/nanoclaw-conferences#4). Each stage now saves
its artifact here, and a continuation the same UTC day reloads it.

Directory: `/workspace/group/state/cfp-run/` (override `CFP_RUN_STATE_DIR`),
holding `manifest.json` = {"run_date": "<YYYY-MM-DD UTC>", "completed":
[stage, ...]} plus one `<stage>.json` per saved stage. Schema doc:
`references/run-state.md`.

Commands (JSON on stdout):
  begin               Resume a run begun today: {"resume": true, "run_date",
                      "completed"}. Anything else (no manifest, another day,
                      unreadable) starts fresh: {"resume": false, ...}.
  save <stage>        Store stdin JSON as <stage>.json and mark it completed in
                      the run's manifest, keeping that run's date: {"saved":
                      stage}.
  load <stage>        Print the saved artifact; exit 2 when there is none.
  invalidate <stage>...
                      Drop the named stages and every stage completed after
                      the earliest of them (later stages derive from earlier
                      ones): {"invalidated": [...], "absent": [...]}.
  done                Remove the run directory: {"cleared": true}.

Resume only saves work: stages are idempotent and Step 5 re-verifies the
cohort, so a fresh run is always safe. Stage names match STAGE_RE.
Exit 0 on success, 1 on bad input or I/O failure, 2 for an absent stage.
"""

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_RUN_DIR = Path("/workspace/group/state/cfp-run")
MANIFEST = "manifest.json"
STAGE_RE = re.compile(r"^(?!manifest\Z)[a-z0-9][a-z0-9_-]*\Z")


class BadInput(Exception):
    """Exit 1 with this message."""


def run_dir() -> Path:
    return Path(os.environ.get("CFP_RUN_STATE_DIR") or DEFAULT_RUN_DIR)


def today() -> str:
    return datetime.now(UTC).date().isoformat()


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def stored(directory: Path) -> dict | None:
    """The manifest on disk, whatever its day, or None when it is missing or
    malformed."""
    try:
        data = json.loads((directory / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (
        isinstance(data, dict)
        and isinstance(data.get("run_date"), str)
        and isinstance(data.get("completed"), list)
        and all(isinstance(s, str) and STAGE_RE.match(s) for s in data["completed"])
    ):
        return {"run_date": data["run_date"], "completed": data["completed"]}
    return None


def manifest(directory: Path) -> dict | None:
    """Today's manifest, or None when there is no run of today to resume."""
    current = stored(directory)
    return current if current is not None and current["run_date"] == today() else None


def clear(directory: Path) -> None:
    if directory.exists():
        for child in directory.iterdir():
            if child.is_file():
                child.unlink()


def stage_name(stage: str) -> str:
    if not STAGE_RE.match(stage):
        raise BadInput(f"invalid stage name {stage!r}")
    return stage


def begin(directory: Path) -> dict:
    current = manifest(directory)
    if current is not None:
        return {"resume": True, **current}
    clear(directory)
    fresh = {"run_date": today(), "completed": []}
    write_json(directory / MANIFEST, fresh)
    return {"resume": False, **fresh}


def save(directory: Path, stage: str, raw: str) -> dict:
    stage_name(stage)
    try:
        artifact = json.loads(raw)
    except ValueError as exc:
        raise BadInput(f"stage {stage!r} stdin is not valid JSON: {exc}") from None
    # A run that crosses midnight keeps its own date and stages; `begin`
    # decides whether to resume it.
    current = stored(directory) or {"run_date": today(), "completed": []}
    write_json(directory / f"{stage}.json", artifact)
    if stage not in current["completed"]:
        current["completed"].append(stage)
    write_json(directory / MANIFEST, current)
    return {"saved": stage}


def load(directory: Path, stage: str) -> str | None:
    stage_name(stage)
    try:
        text = (directory / f"{stage}.json").read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as exc:
        raise BadInput(f"cannot read stage {stage!r}: {exc}") from None
    try:
        json.loads(text)
    except ValueError as exc:
        raise BadInput(f"saved stage {stage!r} is corrupt JSON: {exc}") from None
    return text if text.endswith("\n") else text + "\n"


def invalidate(directory: Path, stages: list[str]) -> dict:
    targets = list(dict.fromkeys(stage_name(s) for s in stages))
    current = stored(directory)
    if current is not None:
        hits = [i for i, s in enumerate(current["completed"]) if s in targets]
        if hits:
            cut = min(hits)
            targets += [s for s in current["completed"][cut:] if s not in targets]
            current["completed"] = current["completed"][:cut]
            # Rewrite before unlinking: a failed write leaves the artifacts intact.
            write_json(directory / MANIFEST, current)
    invalidated, absent = [], []
    for stage in targets:
        try:
            (directory / f"{stage}.json").unlink()
            invalidated.append(stage)
        except FileNotFoundError:
            absent.append(stage)
    return {"invalidated": invalidated, "absent": absent}


def done(directory: Path) -> dict:
    clear(directory)
    if directory.exists():
        directory.rmdir()
    return {"cleared": True}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Same-day check-cfps checkpoints.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("begin")
    sub.add_parser("save").add_argument("stage")
    sub.add_parser("load").add_argument("stage")
    sub.add_parser("invalidate").add_argument("stages", nargs="+")
    sub.add_parser("done")
    args = parser.parse_args(argv)
    directory = run_dir()
    try:
        if args.command == "load":
            text = load(directory, args.stage)
            if text is None:
                sys.stderr.write(f"run-state: no saved artifact for stage {args.stage!r}\n")
                return 2
            sys.stdout.write(text)
            return 0
        if args.command == "begin":
            result = begin(directory)
        elif args.command == "save":
            result = save(directory, args.stage, sys.stdin.read())
        elif args.command == "invalidate":
            result = invalidate(directory, args.stages)
        else:
            result = done(directory)
    except BadInput as exc:
        sys.stderr.write(f"run-state: {exc}\n")
        return 1
    except OSError as exc:
        sys.stderr.write(f"run-state: {args.command} failed: {type(exc).__name__}: {exc}\n")
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
