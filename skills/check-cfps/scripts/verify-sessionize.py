#!/usr/bin/env python3
"""Deterministic Sessionize verification driver for check-cfps Step 5.

This single script collapses the three former Step-5 sub-steps —
`prepare-sessionize-batch.py` -> the `sessionize_get_events` round-trip ->
`apply-sessionize-results.py` — into one invocation the skill runs with no
discretion over the network call.

Why a driver, not three prose steps: the round-trip used to be an MCP tool
(`mcp__nanoclaw__sessionize_get_events`) the agent invoked, which meant the
~267 KB response landed in the agent's own context. Under token pressure a
Haiku maintenance run skipped the call "to save tokens," fabricated verdicts
from memory, and still recorded success (jbaruch/nanoclaw-conferences#7). A
Python subprocess cannot invoke an MCP tool, so the fix is to make the call a
deterministic HTTP request *inside this script* against the host-provided
Sessionize API — the payload never enters the agent context, so there is
nothing in-context to skip.

The call is the same normalized contract the MCP tool returned, so this driver
reuses the existing prepare/apply logic verbatim (sibling importlib load, the
same mechanism prepare-sessionize-batch.py already uses for backfill-source.py):

  prepare(entries)            -> routing + slug derivation (prep object)
  POST {base}/events {slugs}  -> the per-slug event array (this script)
  apply_results(prep, array)  -> one deterministic decision per entry

Config (host-injected env, see README): `SESSIONIZE_API_BASE` (required when
there are slugs to verify) and `SESSIONIZE_API_TOKEN` (optional; sent as
`Authorization: Bearer <token>` when present). The MCP Sessionize tools remain
available for ad-hoc queries; only this pipeline path moved off them.

Verification evidence: every run writes `verify-evidence.json` into the
run-state dir (`$CFP_RUN_STATE_DIR`, default
`/workspace/group/state/cfp-run/`) recording whether a live call actually
happened this run. `stamp-last-checked.py` reads it and refuses to advance the
`_last_checked` heartbeat when no live verification occurred — so a run that
skipped (or could not reach) the API cannot report a clean success
(jbaruch/nanoclaw-conferences#8).

Failure handling — never substitute remembered verdicts:
  * API unreachable / HTTP error / non-JSON / non-array response for a
    non-empty slug set: treat as a cohort-wide verification failure — apply
    sees no results, so every Sessionize entry resolves to `verify_failed`
    (Step 8 persists the `⚠️ STALE DATA` markers, `last_verified` untouched),
    and `live_call` is recorded false so the stamp gate keeps the run from
    reporting clean. Exit 0: the verify_failed decisions ARE the product Step 8
    must persist.
  * Per-slug `{slug, error}` inside an otherwise live response: handled by
    apply as before (that entry -> verify_failed); the call still happened, so
    `live_call` is true.

Input (stdin, JSON array) — the cohort to verify, identical to
prepare-sessionize-batch.py's input:
  [{"id", "cohort": "new"|"stored", "cfp_url", "source"?, "slug"?}, ...]

Output (stdout, JSON):
  {"prep": <prepare() output>,
   "results": [<event array, [] when the live call failed/was skipped>],
   "decisions": [...], "summary": {...},      # from apply_results()
   "non_sessionize": ["<id>", ...],           # caller marks _verified_this_run
   "evidence": {"run_date", "slugs_expected", "live_call",
                "verified", "dismissed", "dropped", "verify_failed"}}

Exit 0 on any handled outcome (live success OR cohort-wide verify failure).
Exit 1 with a stderr diagnostic on unusable input (non-JSON / not an array /
malformed entry) or missing `SESSIONIZE_API_BASE` when there are slugs to
verify or a failure writing the evidence marker.
"""

import argparse
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RUN_DIR = Path("/workspace/group/state/cfp-run")
EVIDENCE_NAME = "verify-evidence.json"
HTTP_TIMEOUT = 30


def _load_sibling(name: str, filename: str):
    """Load a sibling script as a module (their filenames contain hyphens, so
    a normal import is impossible). Same importlib mechanism the sibling
    scripts already use among themselves, keeping one definition of the
    prepare/apply logic rather than a divergent copy here."""
    sibling = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, sibling)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load sibling module from {sibling}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_prepare_mod = _load_sibling("_cfps_prepare_batch", "prepare-sessionize-batch.py")
_apply_mod = _load_sibling("_cfps_apply_results", "apply-sessionize-results.py")
prepare = _prepare_mod.prepare
apply_results = _apply_mod.apply_results


def _run_dir() -> Path:
    override = os.environ.get("CFP_RUN_STATE_DIR")
    return Path(override) if override else DEFAULT_RUN_DIR


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def fetch_events(base: str, token: str | None, slugs: list[str]) -> list:
    """POST the slug list to the host Sessionize API and return the parsed
    event array. Raises on transport/HTTP/decode failure or a non-array body
    — the caller maps any raise to a cohort-wide verification failure. Calls
    the module-global `urllib.request.urlopen` so tests can monkeypatch it
    (the conftest pattern shared with check-cfps-fetch.py)."""
    url = base.rstrip("/") + "/events"
    body = json.dumps({"slugs": slugs}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(
            f"events endpoint returned {type(payload).__name__}, expected a JSON array"
        )
    return payload


def _write_evidence(run_dir: Path, evidence: dict) -> None:
    """Persist the verification-evidence marker the stamp gate reads. A write
    failure is not swallowed — it raises and the caller exits non-zero, since a
    missing marker makes the stamp gate (correctly) refuse to advance the
    heartbeat rather than silently advancing it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / EVIDENCE_NAME).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def run_verification(prep: dict, base: str | None, token: str | None) -> tuple[list, bool]:
    """Perform the live events round-trip for `prep`'s slug set.

    Returns `(results, live_call)`. An empty slug set needs no call (returns
    `([], False)`). A failed call for a non-empty slug set is a handled,
    cohort-wide verification failure: returns `([], False)` after writing a
    stderr diagnostic, so apply resolves every Sessionize entry to
    `verify_failed` rather than this script fabricating verdicts."""
    slugs = prep["slugs"]
    if not slugs:
        return [], False
    # `base` is guaranteed non-None here: main() rejects an empty slug set with
    # an unset SESSIONIZE_API_BASE before calling this.
    assert base is not None
    try:
        return fetch_events(base, token, slugs), True
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"verify-sessionize: live events call failed: {type(exc).__name__}: {exc}\n"
        )
        return [], False


def drive(prep: dict, base: str | None, token: str | None, run_dir: Path) -> dict:
    """live events call -> apply, writing the evidence marker. The single
    apply/evidence path: a failed call simply yields no results, which apply
    turns into cohort-wide verify_failed. `prep` is `prepare()`'s output,
    built once by the caller."""
    results, live_call = run_verification(prep, base, token)

    applied = apply_results(prep, results)
    summary = applied["summary"]
    evidence = {
        "run_date": _today(),
        "slugs_expected": len(prep["slugs"]),
        "live_call": live_call,
        "verified": summary["verified"],
        "dismissed": summary["dismissed"],
        "dropped": summary["dropped"],
        "verify_failed": summary["verify_failed"],
    }
    _write_evidence(run_dir, evidence)

    return {
        "prep": prep,
        "results": results,
        "decisions": applied["decisions"],
        "summary": summary,
        "non_sessionize": prep["non_sessionize"],
        "evidence": evidence,
    }


def _validate_entries(entries: object) -> str | None:
    """Return a diagnostic string for the first malformed entry, else None.
    Mirrors prepare-sessionize-batch.py's main() validation so a bad cohort
    fails loudly here instead of silently mis-routing. Takes `object` because
    it validates raw `json.loads` output before the type is known."""
    if not isinstance(entries, list):
        return f"expected a JSON array of cohort entries, got {type(entries).__name__}"
    for entry in entries:
        if not isinstance(entry, dict):
            return f"every entry must be an object, got {type(entry).__name__}"
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            return f"every entry needs a non-empty string `id`; got {entry_id!r}"
        cohort = entry.get("cohort")
        if cohort not in ("new", "stored"):
            return f'entry {entry_id!r} needs `cohort` of "new" or "stored"; got {cohort!r}'
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Sessionize verification driver (prepare -> events -> apply)."
    )
    parser.add_argument("--run-dir", type=Path, default=None)
    args = parser.parse_args(argv)
    run_dir = args.run_dir if args.run_dir is not None else _run_dir()

    raw = sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"verify-sessionize: stdin is not valid JSON: {exc}\n")
        return 1
    diagnostic = _validate_entries(entries)
    if diagnostic is not None:
        sys.stderr.write(f"verify-sessionize: {diagnostic}\n")
        return 1

    # Slug count gates the SESSIONIZE_API_BASE requirement: a cohort with no
    # Sessionize slugs (all non_sessionize / unverifiable) verifies without a
    # network call, so it must not hard-fail on missing config.
    prep = prepare(entries)
    base = os.environ.get("SESSIONIZE_API_BASE")
    token = os.environ.get("SESSIONIZE_API_TOKEN")
    if prep["slugs"] and not base:
        sys.stderr.write(
            f"verify-sessionize: SESSIONIZE_API_BASE is unset but {len(prep['slugs'])} "
            "slug(s) need live verification\n"
        )
        return 1

    try:
        output = drive(prep, base, token, run_dir)
    except OSError as exc:
        sys.stderr.write(
            f"verify-sessionize: could not write evidence marker: {type(exc).__name__}: {exc}\n"
        )
        return 1

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
