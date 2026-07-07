#!/usr/bin/env python3
"""Backfill the `source` field on cfp-state.json entries that pre-date source tracking.

Inspects each slug's `cfp_url` host and assigns:
  sessionize.com (any subdomain)         -> "sessionize-speaker-api"
  developers.events (any subdomain)      -> "developers.events"
  javaconferences.org (any subdomain)    -> "javaconferences.org"

Entries whose host doesn't match any known feed are left unsourced; Step 5
treats unsourced entries as non-Sessionize and skips the live API call (the
safe default — won't false-stale).

Idempotent. Entries that already carry a `source` value are left alone.
The read-modify-write runs under the shared advisory lock (state_lock.py)
so concurrent writers cannot lose updates.

Usage:
  python3 backfill-source.py [--state-path /path/to/cfp-state.json]

Output (stdout, JSON last line):
  {
    "backfilled":              <int>,
    "skipped_existing_source": <int>,
    "unsourced_remaining":     <int>,
    "by_source":               {"sessionize-speaker-api": N, ...}
  }

Exit code 0 on success (including state-file-not-found, which is a no-op),
non-zero on read/write failure (with diagnostic on stderr).
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse


def _load_state_lock():
    """Reuse the shared advisory-lock module from the sibling
    state_lock.py so the cfp-state write discipline has exactly one
    definition (same reuse pattern as backfill-name.py's
    `_load_dedup_helpers`)."""
    sibling = Path(__file__).with_name("state_lock.py")
    spec = importlib.util.spec_from_file_location("_cfps_state_lock", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load state_lock.py from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling module next to "
            "backfill-source.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")

KNOWN_HOSTS = (
    ("sessionize.com", "sessionize-speaker-api"),
    ("developers.events", "developers.events"),
    ("javaconferences.org", "javaconferences.org"),
)


def infer_source(cfp_url: str) -> str | None:
    """Return the canonical source string for a CFP URL, or None if unknown."""
    if not cfp_url or not isinstance(cfp_url, str):
        return None
    try:
        host = (urlparse(cfp_url).hostname or "").lower()
    except ValueError:
        return None
    if not host:
        return None
    for known, source in KNOWN_HOSTS:
        if host == known or host.endswith("." + known):
            return source
    return None


def backfill(state: dict) -> tuple[int, int, int, Counter]:
    """Mutate `state` in place.

    Return (backfilled, skipped_existing, unsourced_remaining, by_source).
    """
    backfilled = 0
    skipped_existing = 0
    unsourced_remaining = 0
    by_source: Counter = Counter()

    for slug, entry in state.items():
        if slug.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        existing = entry.get("source")
        if existing:
            skipped_existing += 1
            by_source[existing] += 1
            continue
        inferred = infer_source(entry.get("cfp_url", ""))
        if inferred:
            entry["source"] = inferred
            backfilled += 1
            by_source[inferred] += 1
        else:
            unsourced_remaining += 1

    return backfilled, skipped_existing, unsourced_remaining, by_source


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill `source` on cfp-state.json entries that pre-date source tracking."
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to cfp-state.json (default: {DEFAULT_STATE_PATH})",
    )
    args = parser.parse_args(argv)

    if not args.state_path.exists():
        sys.stderr.write(
            f"backfill-source: state file not found at {args.state_path} — nothing to backfill\n"
        )
        print(
            json.dumps(
                {
                    "backfilled": 0,
                    "skipped_existing_source": 0,
                    "unsourced_remaining": 0,
                    "by_source": {},
                }
            )
        )
        return 0

    try:
        with state_lock.locked(args.state_path):
            try:
                state = json.loads(args.state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                sys.stderr.write(
                    f"backfill-source: failed to read {args.state_path}: "
                    f"{type(exc).__name__}: {exc}\n"
                )
                return 1

            if not isinstance(state, dict):
                sys.stderr.write(
                    f"backfill-source: {args.state_path} root is "
                    f"{type(state).__name__}, expected dict; aborting\n"
                )
                return 1

            backfilled, skipped_existing, unsourced_remaining, by_source = backfill(state)

            if backfilled > 0:
                # Atomic write via temp file + os.replace — main groups run default
                # and maintenance containers concurrently against the same
                # /workspace/group/ directory, so a plain write_text would race
                # with check-cfps or morning-brief --mark-shown writes; whichever
                # write loses the race silently drops the other's changes. The
                # temp file lives in the same directory as the target so the
                # final os.replace stays on a single filesystem (replace is only
                # atomic within one filesystem) and the partial file is never
                # visible at the target path.
                target = args.state_path
                try:
                    fd, tmp_path = tempfile.mkstemp(
                        prefix=target.name + ".",
                        suffix=".tmp",
                        dir=str(target.parent),
                    )
                    try:
                        with os.fdopen(fd, "w", encoding="utf-8") as fh:
                            fh.write(json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                        os.replace(tmp_path, target)
                    except OSError:
                        # Best-effort cleanup of the orphan temp file; the outer
                        # except still fires with the original error.
                        try:
                            os.unlink(tmp_path)
                        except FileNotFoundError:
                            pass
                        raise
                except OSError as exc:
                    sys.stderr.write(
                        f"backfill-source: failed to write {target}: {type(exc).__name__}: {exc}\n"
                    )
                    return 1

            print(
                json.dumps(
                    {
                        "backfilled": backfilled,
                        "skipped_existing_source": skipped_existing,
                        "unsourced_remaining": unsourced_remaining,
                        "by_source": dict(by_source),
                    }
                )
            )
            return 0
    except state_lock.LockTimeout as exc:
        sys.stderr.write(f"backfill-source: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
