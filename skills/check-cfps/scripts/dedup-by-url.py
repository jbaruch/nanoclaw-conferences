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
       b) Else: `shown_in_brief: true` (sticky) wins — the previously
          surfaced CFP carries `status`/`bot_notes` semantics the
          brief relies on. When multiple sticky entries collide, the
          c) → e) chain runs within the sticky cohort.
       c) Else: the slug whose `source` matches the URL's host
          (sessionize.com → sessionize-speaker-api, etc.) — that row
          carries authoritative API-driven metadata.
       d) Else: the slug whose `source` ranks highest in
          SOURCE_PRIORITY (javaconferences.org > sessionize-speaker-api
          > developers.events > anything else). Self-hosted CFP URLs
          (`*.cfp.dev`, a conference's own domain) match no known host,
          so without this tier an alphabetical accident could keep the
          developers.events copy and silently drop the
          javaconferences.org source tag that drives Tier-1
          auto-approve (jbaruch/nanoclaw-conferences#25).
       e) Else: the alphabetically-earliest slug, for determinism.
  3. Merge non-overlapping `bot_notes` from each loser into the winner.
     Skipped when the winner is `user_actioned: true` (immutability)
     or `shown_in_brief: true` (the Step 8 sticky rule preserves
     `bot_notes`).
  4. Fill the winner's unusable `name`, `city`, and `conf_date` from
     the losers (first loser in sorted order with a usable value —
     "usable" meaning a non-empty, non-whitespace string; junk shapes
     from schema drift count as missing). A merge must never discard
     the only copy that carried a `name` — a nameless survivor is
     invisible to the priority matcher and the brief
     (jbaruch/nanoclaw-conferences#23/#25). Skipped when the winner is
     `user_actioned: true` (immutability); sticky winners only have
     `status`/`bot_notes` protected, so gap-fill applies to them.
  5. Delete the loser keys.

Idempotent. A second run sees no remaining collisions and is a no-op.
The read-modify-write runs under the shared advisory lock (state_lock.py)
so concurrent writers cannot lose updates.

Usage:
  python3 dedup-by-url.py [--state-path /path/to/cfp-state.json]

Output (stdout, JSON last line):
  {
    "groups_merged":    <int>,   # number of normalised URLs that had >1 slug
    "slugs_dropped":    <int>,   # losers removed
    "notes_merged":     <int>,   # losers whose bot_notes were appended to the winner
    "fields_inherited": <int>,   # missing winner fields filled from losers
    "skipped_multi_user_actioned": <int>,  # groups with >1 user_actioned entries (manual review)
    "merges":           [<merge-record>]   # per-group provenance
  }

Each `<merge-record>`:
  {"url": "<normalised>", "winner": "<slug>", "losers": ["<slug>", ...]}

Exit 0 on success (including state-file-not-found, which is a no-op).
Exit 1 on read/write failure (with diagnostic on stderr).
"""

import argparse
import importlib.util
import json
import os
import sys
import tempfile
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
            "dedup-by-url.py (or reinstall the tile) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

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

# Winner-selection tier (c): when no slug's source matches the URL's
# host (self-hosted CFP forms — `*.cfp.dev`, a conference's own
# domain), prefer the copy from the feed whose attribution carries the
# most downstream weight. javaconferences.org outranks everything
# because its source tag drives the Tier-1 auto-approve and the `java`
# priority interest (jbaruch/nanoclaw-conferences#25); Sessionize
# outranks developers.events because its rows carry API-driven
# metadata. Unknown sources rank last.
SOURCE_PRIORITY = (
    "javaconferences.org",
    "sessionize-speaker-api",
    "developers.events",
)

# Merge step: fields the winner inherits from its losers when the
# winner's own value is missing or empty. `name` is the load-bearing
# one — match-priorities.py and the brief are blind to a nameless
# record (jbaruch/nanoclaw-conferences#23). `deadline` is deliberately
# absent: a loser's deadline may be stale and must never overwrite or
# fill the winner's.
INHERITABLE_FIELDS = ("name", "city", "conf_date")


def _source_rank(source: object) -> int:
    """Position of `source` in SOURCE_PRIORITY; unknown/missing
    sources rank after every known one."""
    if isinstance(source, str) and source in SOURCE_PRIORITY:
        return SOURCE_PRIORITY.index(source)
    return len(SOURCE_PRIORITY)


def _usable(value: object) -> bool:
    """True for a non-empty, non-whitespace string. Whitespace-only
    strings and non-string junk (schema drift) count as missing —
    both for deciding whether the winner needs a fill AND for whether
    a loser's value is worth inheriting; otherwise a `name: "  "` or
    `name: [...]` winner would block inheritance and stay effectively
    nameless downstream."""
    return isinstance(value, str) and bool(value.strip())


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
         source-host-match → source-priority → alphabetical chain runs
         WITHIN the sticky cohort so we still pick the authoritative
         one.
      c) `source` matches the URL's host — authoritative-API row.
      d) Highest-ranking `source` per SOURCE_PRIORITY — preserves the
         javaconferences.org attribution (and its Tier-1 auto-approve)
         when the CFP URL is self-hosted and rule (c) can't fire
         (jbaruch/nanoclaw-conferences#25).
      e) Alphabetically-earliest slug — deterministic tiebreak.
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
    best_rank = min(_source_rank(state[s].get("source")) for s in candidate_pool)
    ranked = [s for s in candidate_pool if _source_rank(state[s].get("source")) == best_rank]
    return sorted(ranked)[0], False


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
    fields_inherited = 0
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
            loser_entry = state[loser]
            loser_notes = loser_entry.get("bot_notes") or ""
            if not skip_notes_merge and loser_notes and loser_notes not in winner_notes:
                sep = " | " if winner_notes else ""
                winner_notes = f"{winner_notes}{sep}[merged from {loser}]: {loser_notes}"
                notes_merged += 1
            # Fill gaps in the winner from the loser before it goes.
            # `user_actioned` winners stay untouched (immutability);
            # sticky winners only have status/bot_notes protected, so
            # metadata gap-fill is allowed on them.
            if not winner_actioned:
                for field in INHERITABLE_FIELDS:
                    if _usable(winner_entry.get(field)):
                        continue
                    value = loser_entry.get(field)
                    if _usable(value):
                        winner_entry[field] = value
                        fields_inherited += 1
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
        "fields_inherited": fields_inherited,
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


def _load_state(state_path: Path) -> tuple[dict | None, int]:
    """Read + shape-validate cfp-state.json. Returns (state, 0) on
    success or (None, exit_code) after writing the stderr diagnostic."""
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"dedup-by-url: failed to read {state_path}: {type(exc).__name__}: {exc}\n"
        )
        return None, 1
    if not isinstance(state, dict):
        sys.stderr.write(
            f"dedup-by-url: {state_path} root is {type(state).__name__}, expected dict; aborting\n"
        )
        return None, 1
    return state, 0


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
                        "fields_inherited": 0,
                        "skipped_multi_user_actioned": 0,
                        "merges": [],
                    }
                )
            )
        return 0

    if args.lookup:
        # Read-only mode: no lock. os.replace already guarantees a
        # complete snapshot, and taking the exclusive lock here would
        # let an unrelated writer block (or time out) a pure lookup.
        state, err = _load_state(args.state_path)
        if state is None:
            return err
        candidate_urls = [line for line in (raw.strip() for raw in sys.stdin) if line]
        print(json.dumps(lookup(state, candidate_urls)))
        return 0

    try:
        with state_lock.locked(args.state_path):
            state, err = _load_state(args.state_path)
            if state is None:
                return err

            result = dedup(state)

            if result["slugs_dropped"] > 0:
                try:
                    _atomic_write(args.state_path, state)
                except OSError as exc:
                    sys.stderr.write(
                        f"dedup-by-url: failed to write {args.state_path}: "
                        f"{type(exc).__name__}: {exc}\n"
                    )
                    return 1

            payload = result
    except state_lock.LockError as exc:
        sys.stderr.write(f"dedup-by-url: {exc}\n")
        return 1

    # Print after releasing the lock — a blocked stdout consumer must not
    # extend the exclusive hold beyond the read-modify-write.
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
