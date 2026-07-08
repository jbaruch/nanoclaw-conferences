#!/usr/bin/env python3
"""Expire stale non-Sessionize CFP entries in cfp-state.json.

The single writer of `status: "expired"` (jbaruch/nanoclaw-conferences#27).
Non-Sessionize sources are deadline-of-record: nothing ever re-checks a
stored `open`/`approved` row against its own deadline, so a closed CFP
lingers as `open` forever — and each run refreshes its `last_verified`,
so it never even ages out via staleness suppression. The Sessionize
branch self-cleans (live API returns closed → dismissed "MISSED"); this
pass covers everything else.

Per entry with `status` in (`open`, `approved`):

  - `user_actioned: true` → NEVER touched (immutability invariant from
    `references/contracts.md`); counted in `skipped_user_actioned`.
  - Effective source `sessionize-speaker-api` (explicit `source`, or
    inferred from the `cfp_url` host when `source` is absent — same
    inference as backfill-source.py) → skipped; the live-verify path
    owns those rows and a stored deadline may be stale against the
    API. Counted in `skipped_sessionize`.
  - `deadline` missing or unparseable (not ISO `YYYY-MM-DD` in its
    first 10 chars) → skipped; counted in `skipped_no_deadline` so
    rot stays visible.
  - `deadline < today` → `status: "expired"` and an idempotent
    `bot_notes` marker (`Expired: CFP deadline <deadline> passed.`).
    A deadline equal to today is still open (submissions close at end
    of deadline day, matching the fetcher's `days_left < 0` drop rule).

Expiry overrides `shown_in_brief` stickiness — same basis as the
Step 8 "Step 5 confirmed closed overrides stickiness" exception: the
deadline-of-record confirms the CFP closed. Revival is free: the
fetcher's state filter only hides `sent`/`dismissed` slugs, so a CFP
re-listed upstream with an extended deadline re-enters as a candidate
and Step 8 rewrites the row from the fresh verdict.

Idempotent: already-`expired` rows are not `open`/`approved`, so a
second run finds nothing to do and does not write. The read-modify-write
runs under the shared advisory lock (state_lock.py) so concurrent
writers cannot lose updates.

Usage:
  python3 expire-cfps.py [--state-path /path/to/cfp-state.json]
                         [--today YYYY-MM-DD]

`--today` (default: the current UTC-naive local date) exists so tests
and replays are clock-independent.

Output (stdout, JSON last line):
  {
    "expired":               <int>,
    "skipped_user_actioned": <int>,
    "skipped_sessionize":    <int>,
    "skipped_no_deadline":   <int>,
    "expired_slugs":         ["<slug>", ...]   # sorted
  }

Exit 0 on success (including state-file-not-found, which is a no-op).
Exit 1 on read/write failure or non-dict root (diagnostic on stderr).
"""

import argparse
import importlib.util
import json
import sys
from datetime import date
from pathlib import Path


def _load_sibling(filename: str, attr: str):
    """Reuse a helper from a sibling script so each rule has exactly
    one definition (the prepare-sessionize-batch.py reuse pattern)."""
    sibling = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(f"_cfps_{attr}", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load {filename} from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling script next to "
            "expire-cfps.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return getattr(module, attr)


# Host → source inference (backfill-source.py owns the table) and the
# concurrent-container atomic write (dedup-by-url.py owns the pattern).
infer_source = _load_sibling("backfill-source.py", "infer_source")
_atomic_write = _load_sibling("dedup-by-url.py", "_atomic_write")


def _load_state_lock():
    """Reuse the shared advisory-lock module from the sibling
    state_lock.py so the cfp-state write discipline has exactly one
    definition (same reuse pattern as `_load_sibling` above)."""
    sibling = Path(__file__).with_name("state_lock.py")
    spec = importlib.util.spec_from_file_location("_cfps_state_lock", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load state_lock.py from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling module next to "
            "expire-cfps.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")

SESSIONIZE_SOURCE = "sessionize-speaker-api"
EXPIRABLE_STATUSES = ("open", "approved")


def effective_source(entry: dict) -> str | None:
    """Explicit `source` if set, else host inference from `cfp_url` —
    mirrors Step 5's source routing so an unsourced sessionize.com row
    is never expired out from under the live-verify path."""
    explicit = entry.get("source")
    if isinstance(explicit, str) and explicit:
        return explicit
    return infer_source(entry.get("cfp_url", ""))


def parse_deadline(value: object) -> date | None:
    """ISO `YYYY-MM-DD` from the first 10 chars, else None."""
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def expire(state: dict, today: date) -> tuple[int, int, int, int, list[str]]:
    """Mutate `state` in place.

    Return (expired, skipped_user_actioned, skipped_sessionize,
    skipped_no_deadline, expired_slugs).
    """
    expired = 0
    skipped_user_actioned = 0
    skipped_sessionize = 0
    skipped_no_deadline = 0
    expired_slugs: list[str] = []

    for slug, entry in state.items():
        if slug.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("status") not in EXPIRABLE_STATUSES:
            continue
        if entry.get("user_actioned") is True:
            skipped_user_actioned += 1
            continue
        if effective_source(entry) == SESSIONIZE_SOURCE:
            skipped_sessionize += 1
            continue
        deadline = parse_deadline(entry.get("deadline"))
        if deadline is None:
            skipped_no_deadline += 1
            continue
        if deadline >= today:
            continue
        entry["status"] = "expired"
        marker = f"Expired: CFP deadline {deadline.isoformat()} passed."
        notes = entry.get("bot_notes")
        notes = notes if isinstance(notes, str) else ""
        if marker not in notes:
            sep = " | " if notes else ""
            entry["bot_notes"] = f"{notes}{sep}{marker}"
        expired += 1
        expired_slugs.append(slug)

    return (
        expired,
        skipped_user_actioned,
        skipped_sessionize,
        skipped_no_deadline,
        sorted(expired_slugs),
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Expire stale non-Sessionize open/approved CFP entries whose "
            "deadline has passed. Idempotent; safe to re-run."
        )
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to cfp-state.json (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--today",
        type=date.fromisoformat,
        default=None,
        help="Override 'today' (ISO YYYY-MM-DD) for clock-independent tests/replays.",
    )
    args = parser.parse_args(argv)
    today = args.today if args.today is not None else date.today()

    if not args.state_path.exists():
        sys.stderr.write(
            f"expire-cfps: state file not found at {args.state_path} — nothing to expire\n"
        )
        print(
            json.dumps(
                {
                    "expired": 0,
                    "skipped_user_actioned": 0,
                    "skipped_sessionize": 0,
                    "skipped_no_deadline": 0,
                    "expired_slugs": [],
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
                    f"expire-cfps: failed to read {args.state_path}: {type(exc).__name__}: {exc}\n"
                )
                return 1

            if not isinstance(state, dict):
                sys.stderr.write(
                    f"expire-cfps: {args.state_path} root is "
                    f"{type(state).__name__}, expected dict; aborting\n"
                )
                return 1

            (
                expired,
                skipped_user_actioned,
                skipped_sessionize,
                skipped_no_deadline,
                expired_slugs,
            ) = expire(state, today)

            if expired > 0:
                try:
                    _atomic_write(args.state_path, state)
                except OSError as exc:
                    sys.stderr.write(
                        f"expire-cfps: failed to write {args.state_path}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    return 1

            payload = {
                "expired": expired,
                "skipped_user_actioned": skipped_user_actioned,
                "skipped_sessionize": skipped_sessionize,
                "skipped_no_deadline": skipped_no_deadline,
                "expired_slugs": expired_slugs,
            }
    except state_lock.LockError as exc:
        sys.stderr.write(f"expire-cfps: {exc}\n")
        return 1

    # Print after releasing the lock — a blocked stdout consumer must not
    # extend the exclusive hold beyond the read-modify-write.
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
