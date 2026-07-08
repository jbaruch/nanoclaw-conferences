#!/usr/bin/env python3
"""Backfill the `name` field on cfp-state.json entries that lack one.

A CFP record without a `name` is invisible to the whole downstream
pipeline: `match-priorities.py` builds its haystack from `name` +
`bot_notes`, Tier-3 relevance analysis has nothing to reason about,
and the brief renders CFPs by name — so a nameless record silently
rots until its deadline lapses (jbaruch/nanoclaw-conferences#23,
Devoxx Morocco expired unsurfaced). Ingestion guarantees a name at
the fetcher, and the dedup merge now inherits the name from a dropped
duplicate, but state damaged before those fixes (or by any future
nameless path) needs a deterministic repair.

Derivation, per nameless entry:

  1. From the slug: strip one trailing `-20\\d\\d` year suffix, split
     on hyphens, capitalize each word — `devoxx-morocco-2026` →
     "Devoxx Morocco". A display fallback, not authoritative: Step 6's
     web search / Sessionize description can refine it later via the
     normal update path.
  2. If the slug yields nothing (all-year or empty), fall back to the
     `cfp_url`'s `<host><path>` (normalised as in dedup-by-url.py).
  3. Neither available → the entry stays nameless and is counted in
     `unnamed_remaining` (surfaced so it doesn't rot invisibly again).

Entries that already carry a non-empty string `name` are left alone.
`user_actioned: true` entries are NEVER touched — the immutability
invariant from `references/contracts.md` reserves them for the user
(only the schema-version stamper may write owner metadata); a
nameless one is counted in `skipped_user_actioned` so it stays
visible without being mutated. Idempotent. Underscore-prefixed config
keys and non-dict entries are skipped (same contract as the sibling
backfill-source.py). The read-modify-write runs under the shared
advisory lock (state_lock.py) so concurrent writers cannot lose
updates.

Usage:
  python3 backfill-name.py [--state-path /path/to/cfp-state.json]

Output (stdout, JSON last line):
  {
    "backfilled":            <int>,
    "skipped_named":         <int>,
    "skipped_user_actioned": <int>,   # nameless but user-owned; never mutated
    "unnamed_remaining":     <int>,
    "named":                 {"<slug>": "<derived name>", ...}
  }

Exit 0 on success (including state-file-not-found, which is a no-op).
Exit 1 on read/write failure or non-dict root (diagnostic on stderr).
"""

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path


def _load_dedup_helpers():
    """Reuse `normalise_url` and `_atomic_write` from the sibling
    dedup-by-url.py so URL normalisation and the concurrent-container
    write discipline each have exactly one definition (same reuse
    pattern as prepare-sessionize-batch.py's `infer_source` load)."""
    sibling = Path(__file__).with_name("dedup-by-url.py")
    spec = importlib.util.spec_from_file_location("_cfps_dedup_by_url", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load dedup-by-url.py from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling script next to "
            "backfill-name.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.normalise_url, module._atomic_write


normalise_url, _atomic_write = _load_dedup_helpers()


def _load_state_lock():
    """Reuse the shared advisory-lock module from the sibling
    state_lock.py so the cfp-state write discipline has exactly one
    definition (same reuse pattern as `_load_dedup_helpers` above)."""
    sibling = Path(__file__).with_name("state_lock.py")
    spec = importlib.util.spec_from_file_location("_cfps_state_lock", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load state_lock.py from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling module next to "
            "backfill-name.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

# One trailing year, either hyphen-joined (`devoxx-morocco-2026`) or
# the whole slug (`2026` — degenerate, falls through to the URL).
_YEAR_SUFFIX = re.compile(r"(^|-)20\d\d$")


def derive_name(slug: str, cfp_url: object) -> str | None:
    """Fallback display name for a nameless record: title-cased slug
    words (year suffix stripped), else the normalised CFP URL, else
    None when neither yields anything."""
    base = _YEAR_SUFFIX.sub("", slug).strip("-")
    words = [w for w in base.split("-") if w]
    if words:
        return " ".join(w.capitalize() for w in words)
    normalised = normalise_url(cfp_url)
    if normalised:
        return normalised
    return None


def backfill(state: dict) -> tuple[int, int, int, int, dict[str, str]]:
    """Mutate `state` in place.

    Return (backfilled, skipped_named, skipped_user_actioned,
    unnamed_remaining, named).
    """
    backfilled = 0
    skipped_named = 0
    skipped_user_actioned = 0
    unnamed_remaining = 0
    named: dict[str, str] = {}

    for slug, entry in state.items():
        if slug.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        existing = entry.get("name")
        if isinstance(existing, str) and existing.strip():
            skipped_named += 1
            continue
        if entry.get("user_actioned") is True:
            # Immutability invariant (references/contracts.md): the
            # user's records are never bot-mutated, not even to add a
            # cosmetic display name. Counted so it stays visible.
            skipped_user_actioned += 1
            continue
        derived = derive_name(slug, entry.get("cfp_url"))
        if derived:
            entry["name"] = derived
            named[slug] = derived
            backfilled += 1
        else:
            unnamed_remaining += 1

    return backfilled, skipped_named, skipped_user_actioned, unnamed_remaining, named


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill `name` on cfp-state.json entries that lack one. Idempotent."
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=Path("/workspace/group/cfp-state.json"),
        help="Path to cfp-state.json (default: /workspace/group/cfp-state.json)",
    )
    args = parser.parse_args(argv)

    if not args.state_path.exists():
        sys.stderr.write(
            f"backfill-name: state file not found at {args.state_path} — nothing to backfill\n"
        )
        print(
            json.dumps(
                {
                    "backfilled": 0,
                    "skipped_named": 0,
                    "skipped_user_actioned": 0,
                    "unnamed_remaining": 0,
                    "named": {},
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
                    f"backfill-name: failed to read {args.state_path}: "
                    f"{type(exc).__name__}: {exc}\n"
                )
                return 1

            if not isinstance(state, dict):
                sys.stderr.write(
                    f"backfill-name: {args.state_path} root is "
                    f"{type(state).__name__}, expected dict; aborting\n"
                )
                return 1

            backfilled, skipped_named, skipped_user_actioned, unnamed_remaining, named = backfill(
                state
            )

            if backfilled > 0:
                try:
                    _atomic_write(args.state_path, state)
                except OSError as exc:
                    sys.stderr.write(
                        f"backfill-name: failed to write {args.state_path}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    return 1

            payload = {
                "backfilled": backfilled,
                "skipped_named": skipped_named,
                "skipped_user_actioned": skipped_user_actioned,
                "unnamed_remaining": unnamed_remaining,
                "named": named,
            }
    except state_lock.LockError as exc:
        sys.stderr.write(f"backfill-name: {exc}\n")
        return 1

    # Print after releasing the lock — a blocked stdout consumer must not
    # extend the exclusive hold beyond the read-modify-write.
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
