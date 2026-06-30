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
deterministic HTTP request *inside this script* — the payload never enters the
agent context, so there is nothing in-context to skip.

The driver calls the same Sessionize universal API the NanoClaw host tool
called, with the same per-slug fetch + normalization (a direct port of the
host's `normalizeSessionizeEvent`, src/ipc.ts), so the array it feeds
`apply_results` is byte-for-byte the `sessionize_get_events` contract apply
already consumes:

  prepare(entries)                      -> routing + slug derivation (prep)
  GET {base}/event?slug=<slug> per slug -> normalized {slug, ...} / {slug,error}
  apply_results(prep, array)            -> one deterministic decision per entry

A per-slug failure (HTTP error, timeout, bad JSON) is isolated to a
`{slug, error}` entry — it never sinks the rest of the cohort — exactly as the
host's `fetchSessionizeEventsBatch` did; apply turns that entry into
`verify_failed`. The MCP Sessionize tools remain available for ad-hoc queries;
only this pipeline path moved off them.

Config (host-injected container env, see README): `SESSIONIZE_EVENT_API_KEY`
(required when there are slugs to verify; sent as the `X-API-KEY` header, the
same key the host's `sessionize_get_events` used) and `SESSIONIZE_API_BASE`
(optional override of the `https://sessionize.com/api/universal` base, for
tests / future proxying).

Verification evidence: every run writes `verify-evidence.json` into the
run-state dir (`$CFP_RUN_STATE_DIR`, default
`/workspace/group/state/cfp-run/`) recording the per-action counts.
`stamp-last-checked.py` reads it and advances the `_last_checked` heartbeat
only when at least one entry was resolved from a live response this run (or
there was nothing to verify) — so a run that skipped the call, or hit a total
Sessionize outage, cannot report a clean success
(jbaruch/nanoclaw-conferences#8).

Failure handling — never substitute remembered verdicts:
  * A slug whose fetch fails -> `{slug, error}` -> apply marks it
    `verify_failed` (Step 8 persists the `⚠️ STALE DATA` markers,
    `last_verified` untouched). A total outage makes every entry verify_failed,
    which the stamp gate reads as "nothing resolved" and refuses to stamp
    clean. Exit 0: the verify_failed decisions ARE the product Step 8 persists.

Input (stdin, JSON array) — the cohort to verify, identical to
prepare-sessionize-batch.py's input:
  [{"id", "cohort": "new"|"stored", "cfp_url", "source"?, "slug"?}, ...]

Output (stdout, JSON):
  {"prep": <prepare() output>,
   "results": [<normalized event array, [] when no slug needed verifying>],
   "decisions": [...], "summary": {...},      # from apply_results()
   "non_sessionize": ["<id>", ...],           # caller marks _verified_this_run
   "evidence": {"run_date", "slugs_expected", "sessionize_total", "live_call",
                "verified", "dismissed", "dropped", "verify_failed"}}

Exit 0 on any handled outcome (live success OR isolated/total verify failure).
Exit 1 with a stderr diagnostic on unusable input (non-JSON / not an array /
malformed entry), a missing `SESSIONIZE_EVENT_API_KEY` when there are slugs to
verify, or a failure writing the evidence marker.
"""

import argparse
import concurrent.futures
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_RUN_DIR = Path("/workspace/group/state/cfp-run")
EVIDENCE_NAME = "verify-evidence.json"
DEFAULT_API_BASE = "https://sessionize.com/api/universal"
HTTP_TIMEOUT = 15
# Bounded concurrency mirrors the host's SESSIONIZE_BATCH_CONCURRENCY so an
# 80-slug cohort doesn't serialize 80 round-trips nor open one socket per slug.
FETCH_CONCURRENCY = 10


def _load_sibling(name: str, filename: str):
    """Load a sibling script as a module (their filenames contain hyphens, so
    a normal import is impossible). Same importlib mechanism the sibling
    scripts already use among themselves, keeping one definition of the
    prepare/apply logic rather than a divergent copy here."""
    sibling = Path(__file__).with_name(filename)
    spec = importlib.util.spec_from_file_location(name, sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load {filename} from {sibling}: the check-cfps script bundle "
            "looks incomplete — restore the sibling script next to verify-sessionize.py "
            "(or reinstall the tile) and retry"
        )
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


def _obj(value: object) -> dict:
    """A nested Sessionize group as a dict, defaulting a missing/non-object
    group to `{}` — the totality the host normalizer relies on so a partial
    payload never raises."""
    return value if isinstance(value, dict) else {}


def _cfp_open(end_utc: object) -> bool:
    """Port of the host's `!!cfpDates.endUtc && new Date(endUtc) > new Date()`:
    the CFP is open when its end instant is parseable and still in the future.
    An unparseable/absent end is closed, matching `new Date(invalid) > now`
    being false."""
    if not isinstance(end_utc, str) or not end_utc:
        return False
    try:
        parsed = datetime.fromisoformat(end_utc.replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed > datetime.now(timezone.utc)


def normalize_event(event: dict, slug: str) -> dict:
    """Map a raw Sessionize universal-event payload to the flat shape
    `apply_results` consumes. Direct port of the host's
    `normalizeSessionizeEvent` (src/ipc.ts) so the contract stays identical now
    that the tile calls the API itself instead of via the MCP tool."""
    cfp_dates = _obj(event.get("cfpDates"))
    event_dates = _obj(event.get("eventDates"))
    location = _obj(event.get("location"))
    timezone_ = _obj(event.get("timezone"))
    return {
        "name": event.get("name"),
        "cfp_open": _cfp_open(cfp_dates.get("endUtc")),
        "cfp_start": cfp_dates.get("startUtc"),
        "cfp_end": cfp_dates.get("endUtc"),
        "cfp_start_local": cfp_dates.get("start"),
        "cfp_end_local": cfp_dates.get("end"),
        "conf_start": event_dates.get("start"),
        "conf_end": event_dates.get("end"),
        "location": location.get("full"),
        "city": location.get("city"),
        "country": location.get("country"),
        "timezone": timezone_.get("iana"),
        "is_online": event.get("isOnline"),
        "website": event.get("website"),
        "cfp_url": event.get("cfpLink") or f"https://sessionize.com/{slug}/",
        "expenses_covered": _obj(event.get("expensesCovered")),
        "organizer": event.get("organizer"),
    }


def fetch_one(base: str, api_key: str, slug: str) -> dict:
    """Fetch and normalize one Sessionize event. Any transport/HTTP/decode
    failure (or a non-object body) is isolated to `{slug, error}` so a single
    bad slug never sinks the cohort — the host's batch contract. Calls the
    module-global `urllib.request.urlopen` so tests can monkeypatch it."""
    url = f"{base.rstrip('/')}/event?slug={urllib.parse.quote(slug)}"
    request = urllib.request.Request(url, headers={"X-API-KEY": api_key}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
            event = json.loads(resp.read().decode("utf-8"))
    except (
        urllib.error.URLError,
        OSError,
        UnicodeDecodeError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        # UnicodeDecodeError (a ValueError subclass) is named explicitly so a
        # non-UTF-8 body is visibly isolated to this slug, not just incidentally
        # caught — matches the repo's decode-failure idiom.
        return {"slug": slug, "error": f"{type(exc).__name__}: {exc}"}
    if not isinstance(event, dict):
        return {
            "slug": slug,
            "error": f"event response was {type(event).__name__}, expected object",
        }
    return {"slug": slug, **normalize_event(event, slug)}


def fetch_events(base: str, api_key: str, slugs: list[str]) -> list:
    """Fetch every slug with bounded concurrency, preserving input order.
    Never raises — per-slug failures are `{slug, error}` entries."""
    if not slugs:
        return []
    workers = min(FETCH_CONCURRENCY, len(slugs))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(lambda slug: fetch_one(base, api_key, slug), slugs))


def _write_evidence(run_dir: Path, evidence: dict) -> None:
    """Persist the verification-evidence marker the stamp gate reads. A write
    failure is not swallowed — it raises and the caller exits non-zero, since a
    missing marker makes the stamp gate (correctly) refuse to advance the
    heartbeat rather than silently advancing it."""
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / EVIDENCE_NAME).write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")


def drive(prep: dict, base: str, api_key: str, run_dir: Path) -> dict:
    """Fetch -> apply, writing the evidence marker. `prep` is `prepare()`'s
    output, built once by the caller; `api_key` is required only when prep has
    slugs (main() enforces that before calling)."""
    slugs = prep["slugs"]
    results = fetch_events(base, api_key, slugs) if slugs else []
    live_call = bool(slugs)

    applied = apply_results(prep, results)
    summary = applied["summary"]
    # `sessionize_total` is the full Sessionize cohort that needed verification
    # this run — entries WITH a fetchable slug (`prep["sessionize"]`) PLUS
    # Sessionize-sourced entries with no derivable slug (`prep["unverifiable"]`,
    # which apply resolves to verify_failed). The stamp gate keys on this, not
    # on `slugs_expected` (unique fetchable slugs): an unverifiable-only cohort
    # has slugs_expected==0 yet must NOT count as "nothing to verify", or a run
    # that resolved nothing would advance the heartbeat (jbaruch/nanoclaw-conferences#8,
    # stateful-artifacts).
    evidence = {
        "run_date": _today(),
        "slugs_expected": len(slugs),
        "sessionize_total": len(prep["sessionize"]) + len(prep["unverifiable"]),
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

    # Slug count gates the API-key requirement: a cohort with no Sessionize
    # slugs (all non_sessionize / unverifiable) verifies without a network call,
    # so it must not hard-fail on missing config.
    prep = prepare(entries)
    base = os.environ.get("SESSIONIZE_API_BASE") or DEFAULT_API_BASE
    api_key = os.environ.get("SESSIONIZE_EVENT_API_KEY")
    if prep["slugs"] and not api_key:
        sys.stderr.write(
            f"verify-sessionize: SESSIONIZE_EVENT_API_KEY is unset but {len(prep['slugs'])} "
            "slug(s) need live verification\n"
        )
        return 1

    try:
        # api_key is non-None here when slugs exist (guarded above); when there
        # are no slugs, drive() makes no call and never reads it.
        output = drive(prep, base, api_key or "", run_dir)
    except OSError as exc:
        sys.stderr.write(
            f"verify-sessionize: could not write evidence marker: {type(exc).__name__}: {exc}\n"
        )
        return 1

    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
