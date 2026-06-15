#!/usr/bin/env python3
"""Apply check-cfps Step 5 Sessionize batch results to the entries.

Consumes the `prepare-sessionize-batch.py` join table plus the array
returned by the `sessionize_get_events` MCP call and emits one
deterministic decision per Sessionize entry. The decision logic mirrors
the Step 5 contract:

  * result missing for a slug, or `{slug, error}`  -> verify_failed
    (caller applies the verification-failure protocol in
    `references/contracts.md`)
  * cfp_open is False  -> new candidate: drop; stored entry: dismiss with
    bot_notes "Dismissed: MISSED — CFP closed on <date>. Verified via
    Sessionize." (a closed stored result with no usable cfp_end_local is
    malformed -> verify_failed, never a "closed on None" dismissal)
  * is_online is True  -> new candidate: drop; stored entry: dismiss with
    bot_notes "Dismissed: online/virtual event."
  * otherwise          -> verified, with the refreshed deadline
    (cfp_end_local[:10]) carried back for Steps 6/8 — UNLESS the success
    payload has no usable `cfp_end_local` (missing / non-string), which is
    treated as verify_failed rather than stamping a null-deadline verified.

One result is fanned out to *every* entry sharing its slug, so a stored
entry and a new candidate that resolve to the same Sessionize slug both
get the same verdict and no duplicate is left unverified. Entries flagged
`unverifiable` by the prep step (Sessionize-sourced but no derivable slug)
carry their cohort and are emitted as verify_failed.

A `verified` decision carries the full Sessionize `event` so the caller
attaches `expenses_covered` and the other event fields for Steps 6/8
without re-joining results to entries.

This script is pure: it computes decisions only and writes nothing to
cfp-state.json. Step 8 remains the sole writer.

Input (stdin, JSON object):
  {"prep": <output of prepare-sessionize-batch.py>,
   "results": [<sessionize_get_events array>]}

Output (stdout, JSON):
  {"decisions": [{"id": ..., "cohort": "new"|"stored",
                  "action": "verified"|"dismiss"|"drop"|"verify_failed",
                  "bot_notes": "<only for dismiss>",
                  "deadline": "<only for verified>",
                  "cfp_end_local": "<only for verified>",
                  "event": {<full result, only for verified>}}, ...],
   "summary": {"verified": N, "dismissed": N, "dropped": N,
               "verify_failed": N}}

Exit 0 on success; exit 1 with a stderr diagnostic on malformed input.
"""

import json
import sys

DISMISS_ONLINE = "Dismissed: online/virtual event."


def _date10(value: str) -> str:
    """First 10 chars of an ISO-ish date string. Callers pass only a
    validated non-empty string (a malformed/absent date is routed to
    verify_failed before reaching here)."""
    return value[:10]


def decide(entry: dict, result: dict | None) -> dict:
    """Map one Sessionize entry + its batch result to a decision."""
    entry_id = entry.get("id")
    cohort = entry.get("cohort")
    is_new = cohort == "new"

    if result is None or "error" in result:
        return {"id": entry_id, "cohort": cohort, "action": "verify_failed"}

    if result.get("cfp_open") is False:
        if is_new:
            return {"id": entry_id, "cohort": cohort, "action": "drop"}
        cfp_end_local = result.get("cfp_end_local")
        if not isinstance(cfp_end_local, str) or not cfp_end_local:
            # Closed, but no closure date to cite — malformed. Don't persist
            # a "closed on None" dismissal; route to the verification-failure
            # protocol like any other unreadable response.
            return {"id": entry_id, "cohort": cohort, "action": "verify_failed"}
        return {
            "id": entry_id,
            "cohort": cohort,
            "action": "dismiss",
            "bot_notes": (
                f"Dismissed: MISSED — CFP closed on {_date10(cfp_end_local)}. "
                f"Verified via Sessionize."
            ),
        }

    if result.get("is_online") is True:
        if is_new:
            return {"id": entry_id, "cohort": cohort, "action": "drop"}
        return {
            "id": entry_id,
            "cohort": cohort,
            "action": "dismiss",
            "bot_notes": DISMISS_ONLINE,
        }

    cfp_end_local = result.get("cfp_end_local")
    if not isinstance(cfp_end_local, str) or not cfp_end_local:
        # Open, in-person, but no usable CFP end date — a malformed success.
        # Refusing to mark it verified keeps Step 8 from stamping
        # `last_verified` on a response we couldn't actually read.
        return {"id": entry_id, "cohort": cohort, "action": "verify_failed"}
    return {
        "id": entry_id,
        "cohort": cohort,
        "action": "verified",
        "deadline": _date10(cfp_end_local),
        "cfp_end_local": cfp_end_local,
        # Carry the full event back so the caller attaches `expenses_covered`
        # and friends for Steps 6/8 without re-joining results to entries.
        "event": result,
    }


def apply_results(prep: dict, results: list) -> dict:
    by_slug: dict[str, dict] = {}
    for result in results:
        if isinstance(result, dict) and isinstance(result.get("slug"), str):
            by_slug[result["slug"]] = result

    decisions: list[dict] = []
    for entry in prep.get("sessionize", []):
        decisions.append(decide(entry, by_slug.get(entry.get("slug"))))

    # Unverifiable entries carry their cohort so the caller applies the
    # cohort-specific verification-failure protocol (stored -> stale, new
    # -> drop), same as a missing/errored result.
    for entry in prep.get("unverifiable", []):
        decisions.append(
            {
                "id": entry.get("id"),
                "cohort": entry.get("cohort"),
                "action": "verify_failed",
            }
        )

    summary = {"verified": 0, "dismissed": 0, "dropped": 0, "verify_failed": 0}
    action_to_key = {
        "verified": "verified",
        "dismiss": "dismissed",
        "drop": "dropped",
        "verify_failed": "verify_failed",
    }
    for decision in decisions:
        summary[action_to_key[decision["action"]]] += 1

    return {"decisions": decisions, "summary": summary}


def main(argv: list[str]) -> int:
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"apply-sessionize-results: stdin is not valid JSON: {exc}\n")
        return 1
    if not isinstance(payload, dict):
        sys.stderr.write(
            "apply-sessionize-results: expected a JSON object with `prep` "
            f"and `results`, got {type(payload).__name__}\n"
        )
        return 1

    prep = payload.get("prep")
    results = payload.get("results")
    if not isinstance(prep, dict):
        sys.stderr.write(
            "apply-sessionize-results: `prep` must be the object emitted by "
            "prepare-sessionize-batch.py\n"
        )
        return 1
    if not isinstance(results, list):
        sys.stderr.write(
            "apply-sessionize-results: `results` must be the " "sessionize_get_events array\n"
        )
        return 1
    for key in ("sessionize", "unverifiable"):
        section = prep.get(key, [])
        if not isinstance(section, list) or not all(isinstance(item, dict) for item in section):
            sys.stderr.write(
                f"apply-sessionize-results: `prep.{key}` must be a list of "
                f"objects as emitted by prepare-sessionize-batch.py\n"
            )
            return 1

    print(json.dumps(apply_results(prep, results)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
