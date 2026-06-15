#!/usr/bin/env python3
"""Prepare the Sessionize batch for check-cfps Step 5.

Given the entries Step 5 must verify (new candidates from Steps 2-4 plus
stored `open`/`approved` rows), this script decides — deterministically —
which ones are Sessionize-sourced, derives each one's Sessionize slug, and
emits the unique slug list to hand to the `sessionize_get_events` MCP call
together with a join table for `apply-sessionize-results.py`.

Routing matches `references/source-routing.md`: an entry's *effective
source* is its explicit `source`, or — when `source` is absent — the host
inference from `cfp_url` (shared with `backfill-source.py`, the single
source of truth for the host table). Only entries whose effective source
is `sessionize-speaker-api` are batched; everything else is the agent's
non-Sessionize branch.

Slug derivation: a new candidate already carries its slug (from Step 2);
a stored entry's slug is the first path segment of `cfp_url`
(`urlparse(cfp_url).path.strip("/").split("/")[0]`), NOT its dict key,
which drifts from the URL slug. A Sessionize entry whose `cfp_url` is
missing or unparseable yields no slug and is reported as unverifiable so
the caller applies the verification-failure protocol to it directly.

Input (stdin, JSON array):
  [{"id": "<state key or candidate id>",
    "cohort": "new" | "stored",
    "cfp_url": "<url>",
    "source": "<source or omitted/null>",
    "slug": "<slug or omitted/null>"}, ...]

Output (stdout, JSON):
  {"slugs": ["<unique sessionize slug>", ...],
   "sessionize": [{"id": ..., "cohort": ..., "slug": ...}, ...],
   "non_sessionize": ["<id>", ...],
   "unverifiable": [{"id": ..., "cohort": ...}, ...],
   "counts": {"sessionize": N, "non_sessionize": N,
              "unverifiable": N, "unique_slugs": N}}

Exit 0 on success; exit 1 with a stderr diagnostic on malformed input
(non-JSON, not an array, an entry that is not an object, an entry without
a non-empty string `id`, or a `cohort` other than "new"/"stored").
"""

import importlib.util
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

SESSIONIZE_SOURCE = "sessionize-speaker-api"


def _load_infer_source():
    """Reuse `infer_source` from the sibling backfill-source.py so the
    host -> source table has exactly one definition (a divergent copy
    here would silently mis-route entries the backfill already handles)."""
    sibling = Path(__file__).with_name("backfill-source.py")
    spec = importlib.util.spec_from_file_location("_cfps_backfill_source", sibling)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.infer_source


infer_source = _load_infer_source()


def effective_source(entry: dict) -> str | None:
    """Explicit `source` if set, else host inference from `cfp_url`."""
    explicit = entry.get("source")
    if explicit:
        return explicit
    return infer_source(entry.get("cfp_url", ""))


def derive_slug(entry: dict) -> str | None:
    """The slug for an entry. A new candidate's own `slug` is authoritative
    (Step 2 extracted it from cfpLink); a stored row's slug is ALWAYS the
    first `cfp_url` path segment — never a passed-in `slug`, which may be the
    drifted dict key and would 404. None when no slug can be derived."""
    if entry.get("cohort") == "new":
        existing = entry.get("slug")
        if isinstance(existing, str) and existing:
            return existing
    cfp_url = entry.get("cfp_url")
    if not cfp_url or not isinstance(cfp_url, str):
        return None
    try:
        parsed = urlparse(cfp_url)
    except ValueError:
        return None
    # Require a real scheme + host. A scheme-less string like
    # "sessionize.com/foo" lands entirely in `.path`, so the first segment
    # would be the host ("sessionize.com") — a bogus slug that 404s. Treat
    # it as no-slug (caller marks it unverifiable). Matches the scheme +
    # netloc gate in audit-sessionize-key-drift.py.
    if not parsed.scheme or not parsed.netloc:
        return None
    first = parsed.path.strip("/").split("/")[0]
    return first or None


def prepare(entries: list) -> dict:
    sessionize: list[dict] = []
    non_sessionize: list[str] = []
    unverifiable: list[dict] = []
    slugs: list[str] = []
    seen_slugs: set[str] = set()

    for entry in entries:
        entry_id = entry.get("id")
        cohort = entry.get("cohort")
        if effective_source(entry) != SESSIONIZE_SOURCE:
            non_sessionize.append(entry_id)
            continue
        slug = derive_slug(entry)
        if slug is None:
            unverifiable.append({"id": entry_id, "cohort": cohort})
            continue
        sessionize.append({"id": entry_id, "cohort": cohort, "slug": slug})
        if slug not in seen_slugs:
            seen_slugs.add(slug)
            slugs.append(slug)

    return {
        "slugs": slugs,
        "sessionize": sessionize,
        "non_sessionize": non_sessionize,
        "unverifiable": unverifiable,
        "counts": {
            "sessionize": len(sessionize),
            "non_sessionize": len(non_sessionize),
            "unverifiable": len(unverifiable),
            "unique_slugs": len(slugs),
        },
    }


def main(argv: list[str]) -> int:
    raw = sys.stdin.read()
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"prepare-sessionize-batch: stdin is not valid JSON: {exc}\n")
        return 1
    if not isinstance(entries, list):
        sys.stderr.write(
            "prepare-sessionize-batch: expected a JSON array of entries, "
            f"got {type(entries).__name__}\n"
        )
        return 1
    for entry in entries:
        if not isinstance(entry, dict):
            sys.stderr.write(
                "prepare-sessionize-batch: every entry must be an object, "
                f"got {type(entry).__name__}\n"
            )
            return 1
        entry_id = entry.get("id")
        if not isinstance(entry_id, str) or not entry_id:
            sys.stderr.write(
                "prepare-sessionize-batch: every entry needs a non-empty "
                f"string `id`; got {entry_id!r}\n"
            )
            return 1
        if entry.get("cohort") not in ("new", "stored"):
            sys.stderr.write(
                f'prepare-sessionize-batch: entry {entry_id!r} needs '
                f'`cohort` of "new" or "stored"; got {entry.get("cohort")!r}\n'
            )
            return 1

    print(json.dumps(prepare(entries)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
