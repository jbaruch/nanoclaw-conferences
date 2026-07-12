#!/usr/bin/env python3
"""Stamp the success cursor for `tessl__nightly-cfp-sync`.

The cursor (`nightly-cfp-sync-cursor.json#last_run`) gates the cadence: the
fire-time precheck rests the next wake-up on it. Advancing it means "this run
was a clean success," so it must NOT advance on a run that skipped Sessionize
verification. A 2026-07-11 run did exactly that — it surfaced pre-existing
`_verify_failed` flags as fresh, never invoked `verify-sessionize.py`, yet
stamped the cursor anyway, resting the 3-day cadence on an unverified pass
(jbaruch/nanoclaw-conferences#49). The SKILL prose that told the agent to skip
the stamp on a skipped run was executed by the agent, not enforced — so it
didn't hold.

The stamp is now *gated on the verification heartbeat*. `stamp-last-checked.py`
(check-cfps, Step 8, runs before this stamp) is the single writer of
`cfp-state.json#_last_checked` and advances it to `now` only when this run's
`verify-evidence.json` marker showed a live verification (or there was nothing
to verify). So a `_last_checked` that advanced *during this wrapper run* is the
committed verdict that verification happened this run. This stamp reads that
verdict rather than re-adjudicating the evidence marker itself: one predicate,
one owner, and no cross-skill coupling to check-cfps' run-state internals.
`_last_checked` is also the field this skill already declares as its `evidence:`
in SKILL.md frontmatter.

The freshness test is *run-specific*, not date-only. `_last_checked` is a
last-seen snapshot (coding-policy: stateful-artifacts — prove freshness, don't
assume it): a date-only "is it today" check would accept an earlier same-day
heartbeat from a *direct* `check-cfps` invocation as evidence for a nightly run
that actually skipped verification, reopening the #49 failure class. So the gate
compares `_last_checked` against `--since`, the instant the wrapper run began
(captured before Step 1). Only a `_last_checked` at/after the run start was
stamped by this run's own verification.

  * `_last_checked` parses to an instant >= `--since` -> advance the cursor
    (exit 0).
  * Marker missing, unreadable, absent `_last_checked`, unparseable, or older
    than the run start -> verification is not evidenced *this run*: do NOT
    advance the cursor, write a skipped payload to stdout, and exit 3 so the
    SKILL takes the verify-skipped path and the next cadence fire retries sooner.

Exit codes: 0 cursor stamped; 2 cursor write failure (diagnostic on stderr);
3 verification not evidenced on the heartbeat for this run (gated refusal,
cursor unchanged).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_CURSOR_PATH = "/workspace/group/state/nightly-cfp-sync-cursor.json"
DEFAULT_STATE_PATH = "/workspace/group/cfp-state.json"
SUPPORTED_SCHEMA = 1
EXIT_NOT_EVIDENCED = 3
_ISO_Z = "%Y-%m-%dT%H:%M:%SZ"


def _atomic_write_text(path: Path, content: str, default_mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        target_mode = os.stat(path).st_mode & 0o777
    except FileNotFoundError:
        target_mode = default_mode

    tmp_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=str(path.parent),
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
            encoding="utf-8",
        ) as tf:
            tmp_path = tf.name
            tf.write(content)
            tf.flush()
            os.fsync(tf.fileno())
        os.chmod(tmp_path, target_mode)
        os.replace(tmp_path, path)
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass


def _parse_instant(value: str) -> datetime:
    """Parse a UTC ISO instant (`YYYY-MM-DDTHH:MM:SSZ`) into an aware datetime."""
    return datetime.strptime(value, _ISO_Z).replace(tzinfo=timezone.utc)


def heartbeat_is_fresh(state_path: Path, since: datetime) -> tuple[bool, str]:
    """Decide whether this run's verification is evidenced on the heartbeat.

    Returns `(fresh, reason)`. `fresh` is True only when `cfp-state.json`'s
    top-level `_last_checked` parses as a UTC instant at or after `since` (the
    instant the wrapper run began). `stamp-last-checked.py` advances
    `_last_checked` only on an evidenced verification, so a value at/after the
    run start is the committed verdict that verification ran *this run* — an
    earlier same-day heartbeat from a direct check-cfps invocation is correctly
    rejected. A missing/unreadable state file, a non-object root, an absent or
    non-string `_last_checked`, or a stale/unparseable value all mean 'not
    evidenced this run' — the caller must not advance the cursor. The reason
    string is surfaced for diagnostics."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        return False, f"cannot read {state_path} ({type(exc).__name__}) — heartbeat unconfirmed"
    if not isinstance(state, dict):
        return False, f"{state_path} root is not a JSON object — heartbeat unconfirmed"
    last = state.get("_last_checked")
    if not isinstance(last, str):
        return False, "cfp-state.json has no _last_checked — verification not evidenced this run"
    try:
        last_dt = _parse_instant(last)
    except ValueError:
        return False, f"_last_checked is not a parseable UTC instant ({last!r})"
    since_iso = since.strftime(_ISO_Z)
    if last_dt < since:
        return (
            False,
            f"_last_checked ({last}) predates this run's start ({since_iso}) — "
            "verification not evidenced this run",
        )
    return True, f"_last_checked ({last}) is at/after run start ({since_iso})"


def stamp(cursor_path: Path, now_utc: datetime) -> dict:
    iso = now_utc.strftime(_ISO_Z)
    record = {"schema_version": SUPPORTED_SCHEMA, "last_run": iso}
    _atomic_write_text(cursor_path, json.dumps(record, indent=2) + "\n")
    return {"status": "stamped", "last_run": iso, "cursor_path": str(cursor_path)}


def _parse_now(value: str | None) -> datetime:
    """Injectable clock seam for the cursor write timestamp. `--now` (UTC ISO
    `...Z`) freezes 'now' for tests per coding-policy: testing-standards;
    production omits it and uses the real UTC clock."""
    if value is None:
        return datetime.now(timezone.utc)
    return _parse_instant(value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--cursor",
        default=os.environ.get("NIGHTLY_CFP_SYNC_CURSOR", DEFAULT_CURSOR_PATH),
        help="Path to the cursor file (default: %(default)s).",
    )
    parser.add_argument(
        "--state",
        default=os.environ.get("CFP_STATE_PATH", DEFAULT_STATE_PATH),
        help="Path to cfp-state.json read for the verification heartbeat (default: %(default)s).",
    )
    parser.add_argument(
        "--since",
        required=True,
        help="Wrapper run-start instant (UTC ISO YYYY-MM-DDTHH:MM:SSZ, captured before Step 1). "
        "The cursor advances only when _last_checked is at/after this instant.",
    )
    parser.add_argument(
        "--now",
        default=None,
        help="Freeze 'now' as a UTC ISO instant (YYYY-MM-DDTHH:MM:SSZ) for tests; "
        "omit in production to use the real clock.",
    )
    args = parser.parse_args()

    try:
        since = _parse_instant(args.since)
    except ValueError:
        parser.error("--since must be a UTC ISO instant of the form YYYY-MM-DDTHH:MM:SSZ")

    try:
        now_utc = _parse_now(args.now)
    except ValueError:
        parser.error("--now must be a UTC ISO instant of the form YYYY-MM-DDTHH:MM:SSZ")

    cursor_path = Path(args.cursor)
    state_path = Path(args.state)

    fresh, reason = heartbeat_is_fresh(state_path, since)
    if not fresh:
        sys.stderr.write(f"stamp-cursor: cursor not advanced — {reason}\n")
        sys.stdout.write(
            json.dumps(
                {
                    "status": "skipped",
                    "verification": "unevidenced",
                    "reason": reason,
                    "cursor_path": str(cursor_path),
                }
            )
            + "\n"
        )
        return EXIT_NOT_EVIDENCED

    try:
        payload = stamp(cursor_path, now_utc)
    except OSError as exc:
        sys.stderr.write(f"stamp-cursor: write failed for {cursor_path}: {exc}\n")
        return 2

    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
