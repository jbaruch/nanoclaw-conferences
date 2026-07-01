#!/usr/bin/env python3
"""Dedup cfp-state.json entries that share a normalised `cfp_url`.

Two ingestion branches (Sessionize speaker-API vs developers.events) can
derive different dict keys for the same conference when the underlying
CFP url is identical (e.g. `codemotion-milan-26` vs `codemotion-milan-2026`
both pointing at `https://sessionize.com/codemotion-milan-26`). Step 8's
writer keys state by slug, so both rows persist and morning-brief-cfp
renders the conference twice with separate verification timelines.

This script collapses such duplicates in a single pass:

  1. Group slugs by normalised `cfp_url`: lowercase host + path, scheme
     and query/fragment dropped, trailing `/` stripped.
  2. For each group with >1 slug, pick a winner:
       a) `user_actioned: true` wins (immutability invariant from
          `references/contracts.md`).
       b) Else: the slug whose `source` matches the URL's host
          (sessionize.com → sessionize-speaker-api, etc.) — that row
          carries authoritative API-driven metadata.
       c) Else: the alphabetically-earliest slug, for determinism.
  3. Merge non-overlapping `bot_notes` from each loser into the winner.
     Skipped when the winner is `user_actioned: true` (immutability).
  4. Delete the loser keys.

Idempotent. A second run sees no remaining collisions and is a no-op.

Usage:
  python3 dedup-by-url.py [--state-path /path/to/cfp-state.json]

Output (stdout, JSON last line):
  {
    "groups_merged":    <int>,   # number of normalised URLs that had >1 slug
    "slugs_dropped":    <int>,   # losers removed
    "notes_merged":     <int>,   # losers whose bot_notes were appended to the winner
    "skipped_multi_user_actioned": <int>,  # groups with >1 user_actioned entries (manual review)
    "merges":           [<merge-record>]   # per-group provenance
  }

Each `<merge-record>`:
  {"url": "<normalised>", "winner": "<slug>", "losers": ["<slug>", ...]}

Exit 0 on success (including state-file-not-found, which is a no-op).
Exit 1 on read/write failure (with diagnostic on stderr).
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")

# Mirrors backfill-source.py's KNOWN_HOSTS. When the winner-selection
# rule "source matches URL host" fires, both sides of the comparison
# resolve against this same table so a sessionize-hosted URL with
# `source: sessionize-speaker-api` is the only authoritative pairing.
KNOWN_HOSTS = (
    ("sessionize.com", "sessionize-speaker-api"),
    ("developers.events", "developers.events"),
    ("javaconferences.org", "javaconferences.org"),
)


def normalise_url(url: object) -> str | None:
    """Return `<host><path>` lowercased, with trailing `/` and the
    URL's scheme / query / fragment dropped. Returns None for empty
    input, non-string input, missing host, or urlparse failure.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if not host:
        return None
    path = (parsed.path or "").rstrip("/")
    return f"{host}{path}"


def source_matches_url_host(source: object, url: object) -> bool:
    """True when `source` is the canonical token for the URL's host
    (or a `*.<host>` subdomain), per the KNOWN_HOSTS table. Used to
    pick the authoritative-source row when two slugs collide."""
    if not source or not isinstance(source, str):
        return False
    if not url or not isinstance(url, str):
        return False
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not host:
        return False
    for known, canonical in KNOWN_HOSTS:
        if (host == known or host.endswith("." + known)) and source == canonical:
            return True
    return False


def _pick_winner(slugs: list[str], state: dict) -> tuple[str | None, bool]:
    """Apply the priority rules to a collision group. Returns
    `(winner_slug, skip)` — `skip=True` signals "multiple user_actioned
    entries on the same URL; manual review needed" and the caller must
    leave the group untouched.

    Priority order (earlier wins):

      a) `user_actioned: true` — immutability invariant from
         `references/contracts.md`. The user's verdict is final.
      b) `shown_in_brief: true` — stickiness. The previously surfaced
         CFP carries `status` + `bot_notes` semantics the brief
         relies on; picking it as winner means those fields survive
         the dedup automatically. If multiple sticky entries collide
         (rare — same URL surfaced twice with sticky), the
         source-host-match → alphabetical chain runs WITHIN the
         sticky cohort so we still pick the authoritative one.
      c) `source` matches the URL's host — authoritative-API row.
      d) Alphabetically-earliest slug — deterministic tiebreak.
    """
    user_actioned = [s for s in slugs if state[s].get("user_actioned") is True]
    if len(user_actioned) > 1:
        return None, True
    if len(user_actioned) == 1:
        return user_actioned[0], False
    # Sticky preservation: prefer a `shown_in_brief: true` entry so
    # the sticky `status` + `bot_notes` semantics survive the dedup
    # without a special-case carry-over rule.
    sticky = [s for s in slugs if state[s].get("shown_in_brief") is True]
    candidate_pool = sticky if sticky else slugs
    source_match = [
        s
        for s in candidate_pool
        if source_matches_url_host(state[s].get("source"), state[s].get("cfp_url"))
    ]
    if source_match:
        # Multiple slugs claiming a source-host match shouldn't happen
        # (only one canonical pairing per host) but pick the
        # alphabetically-earliest for determinism if it does.
        return sorted(source_match)[0], False
    return sorted(candidate_pool)[0], False


def dedup(state: dict) -> dict:
    """Mutate `state` in place. Return a counter dict matching the
    documented stdout JSON shape (sans the outer keys the caller
    composes)."""
    by_url: dict[str, list[str]] = {}
    for slug, entry in state.items():
        if slug.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        normalised = normalise_url(entry.get("cfp_url"))
        if not normalised:
            continue
        by_url.setdefault(normalised, []).append(slug)

    groups_merged = 0
    slugs_dropped = 0
    notes_merged = 0
    skipped_multi_user_actioned = 0
    merges: list[dict] = []

    for normalised_url, slugs in by_url.items():
        if len(slugs) < 2:
            continue
        winner, skip = _pick_winner(slugs, state)
        if skip:
            skipped_multi_user_actioned += 1
            sys.stderr.write(
                f"dedup-by-url: skipping {normalised_url}: multiple user_actioned "
                f"entries {sorted(slugs)}; manual review needed\n"
            )
            continue
        assert winner is not None  # _pick_winner only returns None with skip=True
        losers = sorted(s for s in slugs if s != winner)
        winner_entry = state[winner]
        winner_actioned = winner_entry.get("user_actioned") is True
        winner_sticky = winner_entry.get("shown_in_brief") is True
        # Both user_actioned and shown_in_brief carry bot_notes
        # preservation guarantees (the latter per the Step 8 sticky
        # rule in SKILL.md). Skip the merge when either applies.
        skip_notes_merge = winner_actioned or winner_sticky
        winner_notes = winner_entry.get("bot_notes") or ""

        for loser in losers:
            loser_notes = state[loser].get("bot_notes") or ""
            if not skip_notes_merge and loser_notes and loser_notes not in winner_notes:
                sep = " | " if winner_notes else ""
                winner_notes = f"{winner_notes}{sep}[merged from {loser}]: {loser_notes}"
                notes_merged += 1
            del state[loser]
            slugs_dropped += 1

        if not skip_notes_merge and winner_notes != (winner_entry.get("bot_notes") or ""):
            winner_entry["bot_notes"] = winner_notes
        groups_merged += 1
        merges.append({"url": normalised_url, "winner": winner, "losers": losers})

    return {
        "groups_merged": groups_merged,
        "slugs_dropped": slugs_dropped,
        "notes_merged": notes_merged,
        "skipped_multi_user_actioned": skipped_multi_user_actioned,
        "merges": merges,
    }


def _atomic_write(target: Path, state: dict) -> None:
    """Sibling temp file + os.replace — main groups run default and
    maintenance containers concurrently against the same
    /workspace/group/ directory, so a plain write_text would race with
    check-cfps or morning-brief --mark-shown writes; whichever write
    loses the race silently drops the other's changes. The temp file
    lives in the same directory as the target so the final os.replace
    stays on a single filesystem (replace is only atomic within one
    filesystem) and the partial file is never visible at the target
    path."""
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
        try:
            os.unlink(tmp_path)
        except FileNotFoundError:
            pass
        raise


def lookup(state: dict, candidate_urls: list[str]) -> dict[str, str | None]:
    """Map each candidate URL to the existing state slug whose
    `cfp_url` normalises to the same `<host><path>`, or `None` when no
    state entry collides. Used by Step 8 of `check-cfps` after the
    in-place dedup pass: in-memory candidates from Steps 2–4 may have
    been derived to slugs that differ from existing-state keys, so
    the skill renames each candidate to its colliding state key (if
    any) before applying the priority rules below. Pulling this out
    of agent prose into the script keeps every deterministic step of
    the dedup pipeline on the script side per
    `coding-policy: script-delegation`.

    Underscore-prefixed config keys and non-dict entries are skipped
    (matches the dedup-mode contract).
    """
    by_url: dict[str, str] = {}
    for slug, entry in state.items():
        if slug.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        normalised = normalise_url(entry.get("cfp_url"))
        if not normalised:
            continue
        # First slug wins under repeated normalised collisions — but
        # `lookup` is meant to be called AFTER the dedup pass that
        # eliminates intra-state collisions, so this shouldn't fire
        # in practice. Defensive: deterministic alphabetical
        # tiebreak so re-runs don't drift.
        existing = by_url.get(normalised)
        if existing is None or slug < existing:
            by_url[normalised] = slug
    result: dict[str, str | None] = {}
    for url in candidate_urls:
        if not isinstance(url, str):
            result[str(url)] = None
            continue
        normalised = normalise_url(url)
        if normalised is None:
            result[url] = None
            continue
        result[url] = by_url.get(normalised)
    return result


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Dedup cfp-state.json entries that share a normalised `cfp_url`. "
            "Idempotent; safe to re-run."
        )
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to cfp-state.json (default: {DEFAULT_STATE_PATH})",
    )
    parser.add_argument(
        "--lookup",
        action="store_true",
        help=(
            "Read newline-separated candidate URLs from stdin (blank lines "
            "skipped) and emit `{<input_url>: <existing_slug_or_null>}` JSON "
            "to stdout instead of running the dedup pass. Used by Step 8 of "
            "check-cfps to delegate the deterministic candidate-key rewrite "
            "to the script per coding-policy: script-delegation."
        ),
    )
    args = parser.parse_args(argv)

    if not args.state_path.exists():
        sys.stderr.write(
            f"dedup-by-url: state file not found at {args.state_path} — nothing to dedup\n"
        )
        if args.lookup:
            # Lookup-mode on a missing state file is also a no-op: no
            # state means no possible collisions; every candidate maps
            # to None.
            candidate_urls = [line for line in (raw.strip() for raw in sys.stdin) if line]
            print(json.dumps({url: None for url in candidate_urls}))
        else:
            print(
                json.dumps(
                    {
                        "groups_merged": 0,
                        "slugs_dropped": 0,
                        "notes_merged": 0,
                        "skipped_multi_user_actioned": 0,
                        "merges": [],
                    }
                )
            )
        return 0

    try:
        state = json.loads(args.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"dedup-by-url: failed to read {args.state_path}: {type(exc).__name__}: {exc}\n"
        )
        return 1

    if not isinstance(state, dict):
        sys.stderr.write(
            f"dedup-by-url: {args.state_path} root is "
            f"{type(state).__name__}, expected dict; aborting\n"
        )
        return 1

    if args.lookup:
        candidate_urls = [line for line in (raw.strip() for raw in sys.stdin) if line]
        print(json.dumps(lookup(state, candidate_urls)))
        return 0

    result = dedup(state)

    if result["slugs_dropped"] > 0:
        try:
            _atomic_write(args.state_path, state)
        except OSError as exc:
            sys.stderr.write(
                f"dedup-by-url: failed to write {args.state_path}: {type(exc).__name__}: {exc}\n"
            )
            return 1

    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
