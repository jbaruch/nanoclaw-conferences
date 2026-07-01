#!/usr/bin/env python3
"""Deterministic schema_version stamper for cfp-state.json (owner migration).

`check-cfps` is the owner skill for `cfp-state.json` per
`coding-policy: stateful-artifacts`. It runs this after writing the state in
Step 8 to stamp `schema_version` on every record, replacing unreliable LLM
hand-stamping: a single deterministic run brings the whole file to the
current version so `morning-brief-cfp.py`'s reader gate (skip
`schema_version != SUPPORTED`) admits every record.

Skips `_`-prefixed config keys (e.g. `_blocked_prefixes`) and any non-dict
value. Idempotent: a record already at SUPPORTED_SCHEMA_VERSION is untouched,
and the file is rewritten only when at least one record changed. Atomic write
(temp + fsync + os.replace, UTF-8, mode-preserving) so an interrupted run
can't truncate the state file.

Output (stdout): JSON `{"total": M, "stamped": N}` — M record dicts seen,
N newly stamped this run. Exit 0 on success; exit non-zero with a stderr
diagnostic when cfp-state.json is missing / unreadable / not a JSON object.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")
SUPPORTED_SCHEMA_VERSION = 1


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


def stamp(state):
    """Stamp SUPPORTED_SCHEMA_VERSION on every record dict in-place. Returns
    (total_records, newly_stamped)."""
    total = 0
    stamped = 0
    for slug, entry in state.items():
        if slug.startswith("_") or not isinstance(entry, dict):
            continue
        total += 1
        if entry.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
            entry["schema_version"] = SUPPORTED_SCHEMA_VERSION
            stamped += 1
    return total, stamped


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Stamp schema_version on every cfp-state record (owner migration)."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    try:
        state = json.loads(args.state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"stamp-schema-version: cannot read {args.state}: {type(exc).__name__}: {exc}\n"
        )
        return 1
    if not isinstance(state, dict):
        sys.stderr.write(
            f"stamp-schema-version: {args.state} root is "
            f"{type(state).__name__}, expected a JSON object\n"
        )
        return 1

    total, stamped = stamp(state)
    if stamped:
        _atomic_write_json(args.state, state)
    print(json.dumps({"total": total, "stamped": stamped}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
