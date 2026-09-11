#!/usr/bin/env python3
"""Apply travel-date warnings to a final CFP working set before commit.

Input on stdin: {"entries": {slug: record}, "exact_dates_known": {slug: bool}}.
The skill judges date availability; this helper never parses dates or researches
conferences. Supply a boolean for every non-user-actioned open/approved/conflict
entry. Other entries, including user-actioned records, are returned unchanged.
Judgments for unknown slugs, missing required judgments, and malformed input
are errors. Optional judgments for preserved entries must still be booleans.

The exact WARNING sentence is removed when dates are known, or reduced to one
copy at the end when unknown. Other note text is retained, with surrounding
whitespace trimmed when updating the warning. A known-date entry
without the warning is returned unchanged, including an absent bot_notes field.
Sticky relevance notes must already have been restored by Step 8's priority
merge; the helper then applies this permitted warning update to those notes.

Output: the updated {slug: record} object, ready for commit-state.py. No files
are read or written and no judgment fields are added to records. Exit 0 on
success; exit 1 with an actionable stderr diagnostic and no stdout on invalid
JSON or input shape. The committer rechecks on-disk user actions under its lock.
"""

import argparse
import json
import sys

WARNING = "Could not verify travel conflict — exact conference dates unknown."
ACTIVE_STATUSES = frozenset({"open", "approved", "conflict"})


def update(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("expected an object with entries and exact_dates_known")
    entries = payload.get("entries")
    judgments = payload.get("exact_dates_known")
    if not isinstance(entries, dict) or not isinstance(judgments, dict):
        raise ValueError("entries and exact_dates_known must be objects")
    for slug, record in entries.items():
        if not isinstance(slug, str) or slug.startswith("_") or not isinstance(record, dict):
            raise ValueError("entries must map non-config slug strings to record objects")
        if "status" in record and not isinstance(record["status"], str):
            raise ValueError(f"status for {slug!r} must be a string")
    for slug, known in judgments.items():
        if slug not in entries or not isinstance(known, bool):
            raise ValueError("exact_dates_known must map existing slugs to booleans")

    result = {}
    for slug, record in entries.items():
        updated = dict(record)
        if record.get("user_actioned") is True or record.get("status") not in ACTIVE_STATUSES:
            result[slug] = updated
            continue
        if slug not in judgments:
            raise ValueError(f"exact_dates_known is missing a judgment for {slug!r}")
        notes = record.get("bot_notes", "")
        if not isinstance(notes, str):
            raise ValueError(f"bot_notes for {slug!r} must be a string")
        known = judgments[slug]
        if not known or WARNING in notes:
            notes = notes.replace(WARNING, "").strip()
            updated["bot_notes"] = notes
        if not known:
            separator = " " if notes else ""
            updated["bot_notes"] = notes + separator + WARNING
        result[slug] = updated
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)
    try:
        result = update(json.load(sys.stdin))
    except (ValueError, UnicodeDecodeError) as exc:
        sys.stderr.write(
            f"update-travel-warnings: {exc} — fix the working set and date judgments, then retry\n"
        )
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
