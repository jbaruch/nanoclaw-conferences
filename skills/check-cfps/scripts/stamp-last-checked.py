#!/usr/bin/env python3
"""Evidence-gated freshness stamper for cfp-state.json's `_last_checked`.

`check-cfps` is the owner skill for `cfp-state.json`. The top-level
`_last_checked` field is the pipeline's freshness heartbeat: the wall-clock
instant the check-cfps pipeline last ran to completion. LLM hand-writing left
it frozen — on 2026-06-13 it still read `2026-04-22` while every record had
refreshed days earlier, which drove a wrong "pipeline stalled ~7 weeks"
diagnosis (jbaruch/nanoclaw-conferences#4). Stamping it deterministically in
Step 8 makes freshness honest and observable.

But an *unconditional* stamp is itself a lie when verification was skipped: a
2026-06-29 run skipped the live Sessionize call yet this stamp advanced
`_last_checked` to 06-29 and the run reported a clean success, while every
Sessionize entry stayed frozen at 06-20 — defeating the honest-freshness fix
and the #601 work-evidence watchdog (jbaruch/nanoclaw-conferences#8). So the
stamp is now *gated on verification evidence*:

  * Advance `_last_checked` (clean) only when `verify-sessionize.py` left a
    `verify-evidence.json` marker for THIS run (`run_date == today`) in which at
    least one entry was resolved from a live response
    (`verified + dismissed + dropped >= 1`), OR there were no Sessionize entries
    to verify (`sessionize_total == 0`). Entries that only failed verification
    do not count, so a total Sessionize outage (every slug erroring) — or a
    cohort that is entirely unverifiable — can't pass the gate.
  * Otherwise — marker absent, stale-dated, or recording only verify failures —
    do NOT advance the heartbeat. Record a distinct `_last_checked_skipped`
    timestamp the watchdog can read and exit non-zero (code 3), so a run with no
    live verification cannot report clean success.

A clean stamp clears any prior `_last_checked_skipped`. Only the two top-level
`_`-prefixed config keys are touched; every CFP record and other config key is
preserved. Atomic write (temp + fsync + os.replace, UTF-8, mode-preserving) so
an interrupted run can't truncate the state file. The read-modify-write runs
under the shared advisory lock (state_lock.py) so concurrent writers cannot
lose updates. The evidence marker lives in the run-state dir
(`$CFP_RUN_STATE_DIR`, default `/workspace/group/state/cfp-run/`); override
the file with `--evidence`.

Output (stdout): JSON. On a clean stamp:
`{"_last_checked": "<iso>", "verification": "live"|"none-required"}`.
On a gated refusal:
`{"_last_checked_skipped": "<iso>", "verification": "skipped", "reason": "<why>"}`.
Exit codes: 0 clean stamp; 1 state file missing / unreadable / not a JSON
object / write failure; 3 verification not evidenced (gated refusal).
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path


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
            "stamp-last-checked.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")
DEFAULT_RUN_DIR = Path("/workspace/group/state/cfp-run")
EVIDENCE_NAME = "verify-evidence.json"
EXIT_NOT_EVIDENCED = 3


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


def _default_evidence_path() -> Path:
    run_dir = os.environ.get("CFP_RUN_STATE_DIR")
    return (Path(run_dir) if run_dir else DEFAULT_RUN_DIR) / EVIDENCE_NAME


def _read_evidence(path: Path) -> dict | None:
    """Return the verify-evidence marker dict, or None when it is absent,
    unreadable, or not a JSON object — all of which the gate treats as 'no
    verification evidence for this run'."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _count(evidence: dict, key: str) -> int:
    value = evidence.get(key)
    return value if isinstance(value, int) else 0


def verification_state(evidence: dict | None, today: str) -> tuple[bool, str]:
    """Decide whether the heartbeat may advance, plus a reason string.

    Returns `(advance, reason)`. `advance` is True only when the marker is from
    today AND (at least one entry was resolved from a live response this run, OR
    there were no Sessionize entries to verify at all). "Resolved" means
    verified/dismissed/dropped — a real verdict derived from live event data;
    entries that only failed verification (verify_failed) do not count, so a
    total Sessionize outage cannot pass the gate.

    The "nothing to verify" branch keys on `sessionize_total` (the full
    Sessionize cohort: fetchable slugs PLUS unverifiable Sessionize entries),
    NOT `slugs_expected` (unique fetchable slugs). A cohort that is entirely
    unverifiable (Sessionize-sourced but no derivable slug -> all verify_failed)
    has slugs_expected==0 yet sessionize_total>0, so it correctly fails the gate
    instead of being waved through as "none-required"
    (jbaruch/nanoclaw-conferences#8, stateful-artifacts). Older markers without
    `sessionize_total` fall back to `slugs_expected` for the count. The reason
    doubles as the stdout `verification` value on success ("live" /
    "none-required") and the refusal explanation otherwise."""
    if evidence is None:
        return False, "no verify-evidence marker for this run"
    if evidence.get("run_date") != today:
        marker_date = evidence.get("run_date")
        return False, f"verify-evidence is stale (run_date={marker_date!r}, today={today})"
    cohort = evidence.get("sessionize_total", evidence.get("slugs_expected"))
    if cohort == 0:
        return True, "none-required"
    resolved = (
        _count(evidence, "verified") + _count(evidence, "dismissed") + _count(evidence, "dropped")
    )
    if resolved >= 1:
        return True, "live"
    return False, "verify-evidence shows no entry resolved from a live Sessionize response this run"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Evidence-gated stamp of top-level _last_checked on cfp-state.json."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument(
        "--evidence",
        type=Path,
        default=None,
        help="verify-evidence.json path (default: $CFP_RUN_STATE_DIR/verify-evidence.json)",
    )
    args = parser.parse_args(argv)
    evidence_path = args.evidence if args.evidence is not None else _default_evidence_path()

    # The evidence marker is read-only input; only the cfp-state
    # read→write below needs the advisory lock.
    evidence = _read_evidence(evidence_path)

    try:
        with state_lock.locked(args.state):
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

            now_dt = datetime.now(timezone.utc)
            now = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            today = now_dt.date().isoformat()
            advance, reason = verification_state(evidence, today)

            if advance:
                state["_last_checked"] = now
                # A clean run supersedes any earlier same-day gated refusal.
                state.pop("_last_checked_skipped", None)
                payload = {"_last_checked": now, "verification": reason}
            else:
                # Heartbeat does NOT advance: record a distinct skipped marker the
                # watchdog reads and exit non-zero so the run can't report clean.
                state["_last_checked_skipped"] = now
                payload = {
                    "_last_checked_skipped": now,
                    "verification": "skipped",
                    "reason": reason,
                }

            try:
                _atomic_write_json(args.state, state)
            except OSError as exc:
                sys.stderr.write(
                    f"stamp-last-checked: cannot write {args.state}: {type(exc).__name__}: {exc}\n"
                )
                return 1
    except state_lock.LockTimeout as exc:
        sys.stderr.write(f"stamp-last-checked: {exc}\n")
        return 1

    print(json.dumps(payload))
    if not advance:
        sys.stderr.write(f"stamp-last-checked: heartbeat not advanced — {reason}\n")
        return EXIT_NOT_EVIDENCED
    return 0


if __name__ == "__main__":
    sys.exit(main())
