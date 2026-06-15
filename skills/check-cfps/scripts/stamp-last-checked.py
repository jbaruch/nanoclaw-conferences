#!/usr/bin/env python3
"""Deterministic freshness stamper for cfp-state.json's `_last_checked`.

`check-cfps` is the owner skill for `cfp-state.json`. The top-level
`_last_checked` field is the pipeline's freshness heartbeat: the wall-clock
instant the check-cfps pipeline last ran to completion, independent of
whether any record changed. LLM hand-writing left it frozen — on
2026-06-13 it still read `2026-04-22` while every record had refreshed days
earlier, which drove a wrong "pipeline stalled ~7 weeks" diagnosis
(jbaruch/nanoclaw-conferences#4). Stamping it deterministically in Step 8
makes freshness honest and observable.

Unlike `stamp-schema-version.py` (idempotent, rewrites only on change),
this script writes unconditionally: a run that changed no record still
checked, so the heartbeat must advance. It sets ONLY the top-level
`_last_checked` key (a `_`-prefixed config field, not a CFP record) and
leaves every record and other config key untouched. Atomic write
(temp + fsync + os.replace, UTF-8, mode-preserving) so an interrupted run
can't truncate the state file.

Output (stdout): JSON `{"_last_checked": "<iso>"}` — the UTC ISO-8601
timestamp (literal `Z` suffix) just written. Exit 0 on success; exit
non-zero with a stderr diagnostic when cfp-state.json is missing /
unreadable / not a JSON object.
"""

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")


def _atomic_write_json(path, payload):
    """Write `payload` as JSON to `path` via temp file + fsync + os.replace,
    preserving the existing file's mode (0644 fallback). Raises on failure;
    cleanup uses try/finally (no broad except) per
    `coding-policy: error-handling`."""
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


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stamp top-level _last_checked on cfp-state.json with the run timestamp."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"stamp-last-checked: cannot read {args.state}: {type(exc).__name__}: {exc}\n"
        )
        return 1
    if not isinstance(state, dict):
        sys.stderr.write(
            f"stamp-last-checked: {args.state} root is "
            f"{type(state).__name__}, expected a JSON object\n"
        )
        return 1

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    state["_last_checked"] = now
    try:
        _atomic_write_json(args.state, state)
    except OSError as exc:
        sys.stderr.write(
            f"stamp-last-checked: cannot write {args.state}: {type(exc).__name__}: {exc}\n"
        )
        return 1
    print(json.dumps({"_last_checked": now}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
