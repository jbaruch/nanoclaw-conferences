#!/usr/bin/env python3
"""Deterministic new-candidate discovery for check-cfps Step 2.

Step 2 used to be the agent calling `mcp__nanoclaw__sessionize_open_cfps` and
parsing the ~260 KB response inline. On 2026-06-29 that inline parse read the
whole payload as "1 event" and yielded 0 new candidates
(jbaruch/nanoclaw-conferences#9). A subprocess can't invoke the MCP tool, so —
like the verify driver — this script makes the call itself against the same
Sessionize universal API the host tool used and parses the full response
deterministically. The payload never enters the agent context, and the
candidate count is produced by code, not improvised Python.

Contract mirrors the host's `sessionize_open_cfps` handler (src/ipc.ts):
GET `{base}/open-cfps` with the `X-API-KEY` header, then the same default
filter the tool applied — exclude online events and user groups
(`filter: {isOnline: false, isUserGroup: false}`). Each surviving event becomes
a candidate in the Step 2 shape:

  {"name", "city", "conf_date", "deadline", "cfp_url", "slug",
   "source": "sessionize-speaker-api"}

with `slug` = last path segment of `cfpLink`, `cfp_url` = `cfpLink`,
`deadline` = `cfpDates.endUtc[:10]`, `city` = `location.city`, and
`conf_date` = `eventDates.start` (the nested universal-event shape the host's
`normalizeSessionizeEvent` reads). Slugs already present in cfp-state.json (any
status) are skipped — Step 2 only surfaces genuinely new candidates; the stored
cohort is re-verified separately in Step 5.

Config (host-injected container env, see README): `SESSIONIZE_SPEAKER_KEY`
(required; sent as `X-API-KEY`, the same key the host's open-cfps handler used)
and `SESSIONIZE_API_BASE` (optional override of the
`https://sessionize.com/api/universal` base, for tests).

Output (stdout, JSON):
  {"candidates": [{...Step 2 candidate...}, ...],
   "counts": {"events_seen", "after_filter", "skipped_existing",
              "skipped_no_slug", "candidates_new"}}

Exit 0 on success; exit 1 with a stderr diagnostic when
`SESSIONIZE_SPEAKER_KEY` is unset, the API call fails (so the skill aborts Step
2 rather than treating an outage as "no new CFPs"), or cfp-state.json is
unreadable.
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")
DEFAULT_API_BASE = "https://sessionize.com/api/universal"
SESSIONIZE_SOURCE = "sessionize-speaker-api"
HTTP_TIMEOUT = 15


def _obj(value: object) -> dict:
    """A nested Sessionize group as a dict, defaulting a missing/non-object
    group to `{}` so a partial payload never raises."""
    return value if isinstance(value, dict) else {}


def _slug_from_cfp_link(cfp_link: object) -> str | None:
    """Last path segment of `cfpLink` (the Step 2 rule). A Sessionize CFP link
    is `https://sessionize.com/<slug>`, so the last segment is the slug. None
    when there is no usable link."""
    if not isinstance(cfp_link, str) or not cfp_link:
        return None
    parsed = urlparse(cfp_link)
    path = parsed.path if parsed.scheme and parsed.netloc else cfp_link
    segments = [segment for segment in path.split("/") if segment]
    return segments[-1] if segments else None


def _deadline(cfp_dates: dict) -> str | None:
    """`cfpDates.endUtc[:10]` when present and string, else None."""
    end_utc = cfp_dates.get("endUtc")
    return end_utc[:10] if isinstance(end_utc, str) and end_utc else None


def build_candidates(events: object, existing_slugs: set[str]) -> dict:
    """Turn a raw open-cfps array into Step 2 candidates, applying the host's
    default filter (drop online / user-group events) and skipping slugs already
    in state. Pure: no network, no state writes."""
    if not isinstance(events, list):
        raise ValueError(f"open-cfps response was {type(events).__name__}, expected a JSON array")

    candidates: list[dict] = []
    after_filter = 0
    skipped_existing = 0
    skipped_no_slug = 0

    for event in events:
        event = _obj(event)
        if event.get("isOnline") or event.get("isUserGroup"):
            continue
        after_filter += 1

        slug = _slug_from_cfp_link(event.get("cfpLink"))
        if slug is None:
            skipped_no_slug += 1
            continue
        if slug in existing_slugs:
            skipped_existing += 1
            continue

        location = _obj(event.get("location"))
        event_dates = _obj(event.get("eventDates"))
        candidates.append(
            {
                "name": event.get("name"),
                "city": location.get("city"),
                "conf_date": event_dates.get("start"),
                "deadline": _deadline(_obj(event.get("cfpDates"))),
                "cfp_url": event.get("cfpLink"),
                "slug": slug,
                "source": SESSIONIZE_SOURCE,
            }
        )

    return {
        "candidates": candidates,
        "counts": {
            "events_seen": len(events),
            "after_filter": after_filter,
            "skipped_existing": skipped_existing,
            "skipped_no_slug": skipped_no_slug,
            "candidates_new": len(candidates),
        },
    }


def fetch_open_cfps(base: str, api_key: str) -> list:
    """GET the open-cfps list from the Sessionize universal API. Raises on
    transport/HTTP/decode failure or a non-array body — the caller turns any
    raise into a non-zero exit so an outage is never read as 'no new CFPs'.
    Calls the module-global `urllib.request.urlopen` so tests can monkeypatch
    it (the conftest pattern shared with check-cfps-fetch.py)."""
    url = f"{base.rstrip('/')}/open-cfps"
    request = urllib.request.Request(url, headers={"X-API-KEY": api_key}, method="GET")
    with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not isinstance(payload, list):
        raise ValueError(f"open-cfps response was {type(payload).__name__}, expected a JSON array")
    return payload


def _existing_slugs(state_path: Path) -> set[str]:
    """The slugs already tracked in cfp-state.json (its record keys; the
    `_`-prefixed config keys are not records). A missing file means a
    first-ever run — no slugs to skip."""
    try:
        raw = state_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return set()
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError(f"{state_path} root is {type(state).__name__}, expected a JSON object")
    return {key for key in state if not key.startswith("_")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic Sessionize open-CFP discovery for check-cfps Step 2."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    args = parser.parse_args(argv)

    api_key = os.environ.get("SESSIONIZE_SPEAKER_KEY")
    if not api_key:
        sys.stderr.write("discover-open-cfps: SESSIONIZE_SPEAKER_KEY is unset\n")
        return 1
    base = os.environ.get("SESSIONIZE_API_BASE") or DEFAULT_API_BASE

    try:
        existing = _existing_slugs(args.state)
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError) as exc:
        sys.stderr.write(
            f"discover-open-cfps: cannot read {args.state}: {type(exc).__name__}: {exc}\n"
        )
        return 1

    try:
        events = fetch_open_cfps(base, api_key)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"discover-open-cfps: open-cfps fetch failed: {type(exc).__name__}: {exc}\n"
        )
        return 1

    print(json.dumps(build_candidates(events, existing)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
