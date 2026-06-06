#!/usr/bin/env python3
"""Propose priority-interest matches for CFP records (check-cfps Step 5).

PATH C of jbaruch/nanoclaw-admin#308. The priority tagging in
`check-cfps` Step 5 is LLM judgment ("hints, not gates"), but under
budget pressure the agent degraded to a buggy heuristic and missed
obvious matches:

- it compared the priority config's `sources` against the CFP's URL
  HOST (`urlparse(cfp_url).hostname`) instead of the stored `source`
  field, so `sources: ["javaconferences.org"]` never fired for a CFP
  whose `source` is `javaconferences.org` but whose `cfp_url` points at
  the conference's own form (`jdd.org.pl`, `pretalx.com`, ...); and
- it matched keywords with regex word boundaries (`\bjava\b`), so
  "JavaCro" / "JConf" failed.

This script does the DETERMINISTIC half: it PROPOSES interest matches
by case-insensitive keyword hit over the CFP's `name` + `bot_notes`
(substring for keywords longer than `SHORT_KEYWORD_MAXLEN`, word-boundary
for short ones like "ai"/"ml" so they don't hit "rails"/"html"), and by
substring over the stored `source` field (NOT the URL host). It
deliberately does NOT:

- apply a `note`'s exclusions (free-text operator intent — LLM judgment,
  e.g. "AgentCon priority only in first-world venues"); or
- add a match a CFP earns by content alone with no keyword/source hit
  (world knowledge — e.g. "Confitura is a Polish Java conference").

`check-cfps` Step 5 takes `proposed_interests` as a strong prior, then
REMOVES entries a `note`/description contradicts and ADDS no-hit matches
by judgment, recording the arbitrated set as `matched_interests`.

Input  (stdin): a JSON array of CFP records, each `{name, source?,
                 bot_notes?}` (extra fields ignored).
Args:   --priorities <path>  cfp-priorities.json (operator-owned).
Output (stdout): a JSON array parallel to the input, each
                 `{"name": <name>, "proposed_interests": [<id>, ...]}`,
                 interest ids in config order, deduped.

Missing/empty priorities file ⇒ every record gets `proposed_interests:
[]` (the skill still owns the "clear matched_interests when no config"
rule). Exit 0 success; 1 on unreadable/malformed priorities (incl. an
interest whose `keywords`/`sources` is not an array); 2 on bad stdin
(not a JSON array, a non-object record, or a record whose
`name`/`source`/`bot_notes` is not a string). Malformed shapes are
rejected at the boundary rather than coerced into character iterables.

Per `coding-policy: file-hygiene` the entry-point logic is guarded by
`if __name__ == "__main__":` so helpers import cleanly into tests.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Keywords at or below this length match on a word boundary, not as a
# raw substring — otherwise "ai" hits "rails" and "ml" hits "html",
# swamping Step 5 with spurious proposals (Copilot, jbaruch/nanoclaw-admin#308).
# Longer keywords keep substring matching so "java" still hits "JavaCro".
SHORT_KEYWORD_MAXLEN = 3


def load_interests(path: str | Path) -> list[dict]:
    """Return the `priority_interests` list from cfp-priorities.json.

    File absent, empty, or whitespace-only ⇒ `[]` (no policy — matches
    the "missing or empty file ⇒ no policy" contract in
    `references/state-management.md`). `priority_interests` missing/empty
    ⇒ `[]` too. Raises `ValueError` on an unreadable file
    (permission/IO/encoding) or malformed JSON / wrong top-level shape so
    the caller exits non-zero with the documented diagnostic rather than
    letting a raw traceback escape the contract — a corrupt or unreadable
    config is an operator error worth surfacing, distinct from a
    deliberately-absent or empty one.
    """
    p = Path(path)
    try:
        text = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"cfp-priorities.json at {p} is unreadable ({type(exc).__name__}: {exc}). "
            f"Fix the file's permissions/encoding, or remove it to disable priority tagging."
        ) from exc
    if not text.strip():
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"cfp-priorities.json at {p} has malformed JSON ({exc}). "
            f"Fix the JSON syntax, or remove the file to disable priority tagging."
        ) from exc
    if not isinstance(data, dict):
        raise ValueError(
            f"cfp-priorities.json at {p} must be a JSON object with a 'priority_interests' "
            f"array — see check-cfps/references/state-management.md for the shape."
        )
    interests = data.get("priority_interests")
    if interests is None:
        return []
    if not isinstance(interests, list):
        raise ValueError(
            f"'priority_interests' in {p} must be a JSON array of interest objects — "
            f"see check-cfps/references/state-management.md for the shape."
        )
    cleaned = [i for i in interests if isinstance(i, dict) and i.get("id")]
    for interest in cleaned:
        for field in ("keywords", "sources"):
            val = interest.get(field)
            if val is not None and not isinstance(val, list):
                raise ValueError(
                    f"interest '{interest['id']}' field '{field}' in {p} must be a JSON array "
                    f"of strings (got {type(val).__name__}). Fix the config — a bare string "
                    f"would be matched character-by-character."
                )
    return cleaned


def _keyword_matches(keyword: str, haystack_lc: str) -> bool:
    """Case-insensitive keyword hit against an already-lowercased haystack.

    Short keywords (≤ `SHORT_KEYWORD_MAXLEN`) match on a word boundary to
    avoid false positives ("ai" in "rails"); longer keywords match as a
    substring so compound names still hit ("java" in "javacro")."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    if len(kw) <= SHORT_KEYWORD_MAXLEN:
        return re.search(rf"\b{re.escape(kw)}\b", haystack_lc) is not None
    return kw in haystack_lc


def propose(record: dict, interests: list[dict]) -> list[str]:
    """Interest ids whose keyword (over name+bot_notes, via
    `_keyword_matches`) OR source hint (substring over the stored
    `source` field — never the URL host) matches `record`, in config
    order, deduped.

    Non-string `name`/`bot_notes`/`source` and non-list `keywords`/
    `sources` are treated as absent — never coerced into a character
    iterable or `.lower()`-ed into a crash. `main()` / `load_interests()`
    reject those shapes at the boundary; these guards are the backstop.
    """

    def _str(field: str) -> str:
        val = record.get(field)
        return val if isinstance(val, str) else ""

    haystack = f"{_str('name')} {_str('bot_notes')}".lower()
    source_lc = _str("source").lower()

    proposed: list[str] = []
    for interest in interests:
        interest_id = interest.get("id")
        if not isinstance(interest_id, str) or interest_id in proposed:
            continue
        keywords = interest.get("keywords")
        keywords = keywords if isinstance(keywords, list) else []
        sources = interest.get("sources")
        sources = sources if isinstance(sources, list) else []
        keyword_hit = any(isinstance(k, str) and _keyword_matches(k, haystack) for k in keywords)
        source_hit = bool(source_lc) and any(
            isinstance(s, str) and s.strip() and s.lower() in source_lc for s in sources
        )
        if keyword_hit or source_hit:
            proposed.append(interest_id)
    return proposed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Propose priority-interest matches for CFP records (deterministic prefilter)."
    )
    parser.add_argument(
        "--priorities",
        default="/workspace/group/cfp-priorities.json",
        help="Path to cfp-priorities.json (default: production path).",
    )
    args = parser.parse_args(argv)

    try:
        interests = load_interests(args.priorities)
    except ValueError as exc:
        sys.stderr.write(f"match-priorities: {exc}\n")
        return 1

    try:
        records = json.load(sys.stdin)
    except json.JSONDecodeError as exc:
        sys.stderr.write(
            f"match-priorities: stdin is not valid JSON ({exc}). Pipe a JSON array of CFP "
            f'records, e.g. echo \'[{{"name": "...", "source": "...", "bot_notes": "..."}}]\' '
            f"| match-priorities.py --priorities <path>.\n"
        )
        return 2
    if not isinstance(records, list):
        sys.stderr.write(
            f"match-priorities: stdin must be a JSON array of CFP records "
            f"({{name, source, bot_notes}}); got {type(records).__name__}.\n"
        )
        return 2

    out = []
    for record in records:
        if not isinstance(record, dict):
            sys.stderr.write(
                f"match-priorities: each CFP record must be a JSON object "
                f"({{name, source, bot_notes}}); got {type(record).__name__}.\n"
            )
            return 2
        for field in ("name", "source", "bot_notes"):
            val = record.get(field)
            if val is not None and not isinstance(val, str):
                sys.stderr.write(
                    f"match-priorities: CFP record field '{field}' must be a string "
                    f"(got {type(val).__name__}). Fix the record shape "
                    f"({{name, source, bot_notes}}).\n"
                )
                return 2
        out.append({"name": record.get("name"), "proposed_interests": propose(record, interests)})

    json.dump(out, sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
