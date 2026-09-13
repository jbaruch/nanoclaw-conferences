#!/usr/bin/env python3
"""
CFP fetch-and-filter pipeline for the check-cfps skill.

Fetches structured CFP data from primary sources, applies hard deterministic filters
(virtual/online, excluded locations, travel conflicts, cfp-state), and outputs
a filtered + sorted JSON list for the skill to reason about relevance and format.

NOT done here (left to AI reasoning in the skill):
  - Conference topic relevance (Web3/blockchain, .NET/PHP/Ruby, etc.)
  - Email actionability

Output JSON:
  {
    "cfps": [
      {
        "name":     "Conference Name",
        "city":     "City, Country",
        "conf_date": "YYYY-MM-DD",  // earliest conf date
        "cfp_url":  "https://...",
        "deadline": "YYYY-MM-DD",
        "days_left": 14,
        "slug":     "conference-name-2026",
        "source":   "developers.events" | "javaconferences.org"
      },
      ...
    ],
    "warnings": ["Source A unreachable", ...],
    "sources": {"developers.events": {...}, "javaconferences.org": {...}},
    "feed_failure": false,
    "checked_at": "2026-03-29T05:00:00Z"
  }

Per-source health (`sources`) exists because a feed whose every record is
malformed used to be indistinguishable from a valid empty feed: the
per-record guards logged to stderr and the source returned `[]` with no
warning, so systematic upstream format drift read as "no open CFPs"
(jbaruch/nanoclaw-conferences#78). Each source reports
`{status, records_received, records_usable, records_malformed,
records_filtered}`. `records_filtered` counts entries the feed's own rules
drop as a normal outcome (a closed CFP, a conference with no CFP link);
`records_malformed` counts entries whose shape or types the parser could
not use (missing name, absent or non-numeric deadline, unparseable date,
non-string location, an entry that raised a record-shape error). Feed-level
failures are
separate: a body that will not decode as JSON and a non-list root are both
`malformed_feed`, distinct from an `unreachable` transport error.
`classify_source` maps those counts to `status` — see its docstring for the
predicate.

`feed_failure` is the single branch point for callers: true when EVERY
source failed to deliver anything usable for a technical reason
(unreachable, unparseable or non-list root, or all-malformed records). A
valid empty feed and a feed whose entries were all legitimately filtered
are NOT failures, and neither is a partially usable feed. The
"web search fallback needed" warning is suppressed under `feed_failure`,
so a format outage is never dressed up as a normal empty result.

Exit code 0 on success. Exit code 1 when cfp-state.json exists but cannot be
read or parsed — the state filter (sent/dismissed/remind/blocked) must not be
silently skipped, so an unreadable state file is a hard failure, never an
empty state.
"""

import json
import re
import sys
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path


def parse_flexible_date(s: str) -> date | None:
    """Parse dates in ISO (2026-04-13) or human-readable (13 April 2026) format.
    Also handles ranges like '2-3 September 2026' by extracting the first date."""
    s = s.strip()
    if not s:
        return None
    # Try ISO first
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        pass
    # Handle ranges: "2-3 September 2026" → "2 September 2026"
    range_match = re.match(r"(\d{1,2})\s*[-–]\s*\d{1,2}\s+(.+)", s)
    if range_match:
        s = f"{range_match.group(1)} {range_match.group(2)}"
    # Try human-readable: "13 April 2026", "April 13, 2026", etc.
    for fmt in ("%d %B %Y", "%B %d, %Y", "%d %b %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


STATE_PATH = Path("/workspace/group/cfp-state.json")
TRAVEL_PATH = Path("/workspace/group/travel-schedule.json")

# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------


def make_slug(name: str, conf_date: str = "", deadline: str = "") -> str:
    """Normalize conference name to a slug including the year.

    Year priority: embedded in the name → `conf_date` year → `deadline`
    year → current year. The wall clock is the last resort only: a
    recurring conference whose feed name omits the year must key under
    the year of its own dates, or it misses its existing cfp-state row
    across year boundaries and dodges sent/dismissed/remind filtering."""
    lower = name.lower().strip()
    year_match = re.search(r"\b(20\d\d)\b", lower)
    if year_match:
        year = year_match.group(1)
        # Drop the matched year token wherever it sits (it is re-appended
        # as the suffix) — stripping only a trailing year would duplicate
        # a mid-name year: "KubeCon 2026 EU" → "kubecon-2026-eu-2026".
        base = (lower[: year_match.start()] + " " + lower[year_match.end() :]).strip()
    else:
        year = ""
        for candidate in (conf_date, deadline):
            parsed = parse_flexible_date(candidate)
            if parsed:
                year = str(parsed.year)
                break
        if not year:
            year = str(date.today().year)
        base = lower
    slug_base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return f"{slug_base}-{year}"


# ---------------------------------------------------------------------------
# Per-source feed health
# ---------------------------------------------------------------------------

SOURCE_A_NAME = "developers.events"
SOURCE_B_NAME = "javaconferences.org"

# What a malformed upstream RECORD can realistically raise as it is read:
# a wrong type where a str/dict/number belongs (AttributeError, TypeError),
# an unparseable or out-of-range value (ValueError, OverflowError), a
# platform epoch limit (OSError), an absent key on a dict-like (KeyError).
# A programming defect raises outside this set and propagates, so a bug in
# this script is never laundered into a "the feed drifted" count.
RECORD_SHAPE_ERRORS = (
    AttributeError,
    TypeError,
    ValueError,
    KeyError,
    OverflowError,
    OSError,
)

# Statuses that mean "this source delivered nothing usable for a technical
# reason". `empty` (nothing published) and `filtered` (everything dropped by
# the feed's own normal rules) are valid outcomes and stay out of this set.
FAILED_STATUSES = frozenset({"unreachable", "malformed_feed", "all_malformed"})


def classify_source(received: int, usable: int, malformed: int) -> str:
    """Map per-record counts to a source status.

    `empty` — the feed carried no entries at all (valid).
    `filtered` — entries arrived, none survived, and none were malformed:
        every one was dropped by a normal rule (closed CFP, no CFP link).
    `all_malformed` — entries arrived, none survived, at least one was
        malformed: the failure mode that used to masquerade as an empty feed.
    `partial` — at least one usable record alongside at least one malformed
        one: usable output plus a format-drift signal.
    `ok` — at least one usable record and nothing malformed."""
    if received == 0:
        return "empty"
    if usable == 0:
        return "all_malformed" if malformed else "filtered"
    return "partial" if malformed else "ok"


def _health(status: str, *, received=0, usable=0, malformed=0, filtered=0) -> dict:
    return {
        "status": status,
        "records_received": received,
        "records_usable": usable,
        "records_malformed": malformed,
        "records_filtered": filtered,
    }


def _finish_source(label: str, name: str, counts: dict, warnings: list) -> dict:
    """Build the health record and append the source-level warning that the
    all-malformed case needs — without it the caller sees only an empty list."""
    status = classify_source(counts["received"], counts["usable"], counts["malformed"])
    if status == "all_malformed":
        warnings.append(
            f"{label} ({name}): all {counts['received']} records unusable "
            f"({counts['malformed']} malformed) — feed format likely changed"
        )
    return _health(
        status,
        received=counts["received"],
        usable=counts["usable"],
        malformed=counts["malformed"],
        filtered=counts["filtered"],
    )


# ---------------------------------------------------------------------------
# Source A: developers.events
# ---------------------------------------------------------------------------


def fetch_developers_events(warnings: list) -> tuple[list, dict]:
    url = "https://developers.events/all-cfps.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        warnings.append(f"Source A ({SOURCE_A_NAME}) unreachable: {e}")
        return [], _health("unreachable")

    # Parsing is separate from fetching: a response that arrived but is not
    # JSON is a format failure, not a transport outage, and the health
    # contract distinguishes the two.
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        # Every way the decoder can fail on input maps to the same outcome,
        # because letting one escape leaves the caller a traceback instead of
        # the health record the contract promises — and takes the other
        # source's results with it. `ValueError` is the family: it covers
        # `JSONDecodeError` (its subclass) and the digit-limit error a
        # 5,000-digit integer literal raises. `RecursionError` is not a
        # `ValueError`, so an absurdly nested document needs naming too.
        warnings.append(f"Source A ({SOURCE_A_NAME}): response is not valid JSON: {e}")
        return [], _health("malformed_feed")

    if not isinstance(data, list):
        warnings.append("Source A: unexpected format (not a list)")
        return [], _health("malformed_feed")

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    results = []
    counts = {"received": len(data), "usable": 0, "malformed": 0, "filtered": 0}

    for entry in data:
        try:
            # A missing or non-numeric deadline is a shape failure; a real
            # timestamp in the past is a closed CFP, which is a normal drop.
            # Presence and type decide that, never truthiness: epoch `0` is a
            # perfectly numeric timestamp (1970) and belongs in the filtered
            # count, not in the one that can escalate to `feed_failure`.
            until_ms = entry.get("untilDate")
            if not isinstance(until_ms, (int, float)) or isinstance(until_ms, bool):
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source A entry has no usable untilDate ({until_ms!r})\n"
                )
                continue
            if until_ms <= now_ms:
                counts["filtered"] += 1
                continue

            # Feed timestamps are UTC epoch ms; convert in UTC explicitly.
            # A naive fromtimestamp uses the host timezone and shifts
            # deadlines by a day on non-UTC runners.
            deadline = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).date()

            conf = entry.get("conf", {})
            name = conf.get("name", "").strip()
            if not name:
                counts["malformed"] += 1
                sys.stderr.write("check-cfps-fetch: source A entry has no conf.name\n")
                continue

            cfp_url = entry.get("link", "") or conf.get("hyperlink", "")
            # `location` is never .strip()ed here, so a non-string would sail
            # past this guard and blow up in is_virtual()'s .lower() — outside
            # any per-entry handler, taking the whole run with it.
            location = conf.get("location", "")
            if not isinstance(location, str):
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source A entry {name!r} has a non-string "
                    f"location ({location!r})\n"
                )
                continue

            # Conference date: first date in conf.date array (ms timestamps)
            conf_dates = conf.get("date", [])
            conf_date = None
            if conf_dates:
                try:
                    conf_date = (
                        datetime.fromtimestamp(min(conf_dates) / 1000, tz=timezone.utc)
                        .date()
                        .isoformat()
                    )
                except (ValueError, OverflowError, OSError, TypeError) as exc:
                    # Narrow to fromtimestamp's real failure modes:
                    # ValueError for out-of-range, OverflowError for
                    # timestamps beyond the platform's time_t range
                    # (happens with bogus ms-vs-s scale mixups from
                    # the feed), OSError for platform limits,
                    # TypeError for non-numeric input. Missing or
                    # malformed conf_date is non-fatal — the entry
                    # still ships without it — but log so repeated
                    # feed-format drift gets noticed. Without
                    # OverflowError in the narrow list, a bad timestamp
                    # falls through to the outer except-Exception and
                    # drops the whole entry, contrary to intent.
                    sys.stderr.write(
                        f"check-cfps-fetch: source A entry {name!r} "
                        f"conf_date unparseable ({conf_dates!r}): "
                        f"{type(exc).__name__}: {exc}\n"
                    )

            results.append(
                {
                    "name": name,
                    "city": location,
                    "conf_date": conf_date or "",
                    "cfp_url": cfp_url,
                    "deadline": deadline.isoformat(),
                    "source": SOURCE_A_NAME,
                }
            )
            counts["usable"] += 1
        except RECORD_SHAPE_ERRORS as exc:
            # Per-entry guard: swallowing one bad entry is right, but
            # count and log it so systematic upstream format changes
            # become visible instead of producing an empty output.
            counts["malformed"] += 1
            sys.stderr.write(
                f"check-cfps-fetch: source A entry skipped ({type(exc).__name__}: {exc})\n"
            )
            continue

    return results, _finish_source("Source A", SOURCE_A_NAME, counts, warnings)


# ---------------------------------------------------------------------------
# Source B: javaconferences.org
# ---------------------------------------------------------------------------


def fetch_javaconferences(warnings: list) -> tuple[list, dict]:
    url = "https://javaconferences.org/conferences.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        warnings.append(f"Source B ({SOURCE_B_NAME}) unreachable: {e}")
        return [], _health("unreachable")

    # Parsing is separate from fetching: a response that arrived but is not
    # JSON is a format failure, not a transport outage, and the health
    # contract distinguishes the two.
    try:
        data = json.loads(body)
    except (ValueError, RecursionError) as e:
        # Every way the decoder can fail on input maps to the same outcome,
        # because letting one escape leaves the caller a traceback instead of
        # the health record the contract promises — and takes the other
        # source's results with it. `ValueError` is the family: it covers
        # `JSONDecodeError` (its subclass) and the digit-limit error a
        # 5,000-digit integer literal raises. `RecursionError` is not a
        # `ValueError`, so an absurdly nested document needs naming too.
        warnings.append(f"Source B ({SOURCE_B_NAME}): response is not valid JSON: {e}")
        return [], _health("malformed_feed")

    if not isinstance(data, list):
        warnings.append("Source B: unexpected format (not a list)")
        return [], _health("malformed_feed")

    today = date.today()
    results = []
    counts = {"received": len(data), "usable": 0, "malformed": 0, "filtered": 0}

    for entry in data:
        try:
            # The feed lists conferences whether or not a CFP is open, so a
            # missing cfpLink is a normal drop, not a shape failure — and a
            # JSON `null` means exactly the same thing. Stripping it blind
            # would raise, count the record malformed, and let a healthy feed
            # full of link-less conferences escalate all the way to
            # `feed_failure`. A truthy non-string is still malformed.
            cfp_link = entry.get("cfpLink") or ""
            if not isinstance(cfp_link, str):
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source B entry has a non-string cfpLink ({cfp_link!r})\n"
                )
                continue
            cfp_link = cfp_link.strip()
            if not cfp_link:
                counts["filtered"] += 1
                continue

            # Past this point the entry claims an open CFP: an absent or
            # unparseable end date is format drift, a past one is a closed CFP.
            cfp_end_str = entry.get("cfpEndDate", "")
            if not cfp_end_str:
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source B entry {cfp_link!r} has a cfpLink "
                    f"but no cfpEndDate\n"
                )
                continue
            deadline = parse_flexible_date(cfp_end_str)
            if not deadline:
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source B entry {cfp_link!r} cfpEndDate "
                    f"unparseable ({cfp_end_str!r})\n"
                )
                continue
            if deadline < today:
                counts["filtered"] += 1
                continue

            name = entry.get("name", "").strip()
            if not name:
                counts["malformed"] += 1
                sys.stderr.write(f"check-cfps-fetch: source B entry {cfp_link!r} has no name\n")
                continue

            location = entry.get("locationName", "")
            if not isinstance(location, str):
                counts["malformed"] += 1
                sys.stderr.write(
                    f"check-cfps-fetch: source B entry {name!r} has a non-string "
                    f"locationName ({location!r})\n"
                )
                continue
            conf_date_str = entry.get("date", "")
            conf_date_parsed = parse_flexible_date(conf_date_str)
            conf_date = conf_date_parsed.isoformat() if conf_date_parsed else ""

            results.append(
                {
                    "name": name,
                    "city": location,
                    "conf_date": conf_date,
                    "cfp_url": cfp_link,
                    "deadline": deadline.isoformat(),
                    "source": SOURCE_B_NAME,
                }
            )
            counts["usable"] += 1
        except RECORD_SHAPE_ERRORS as exc:
            # Per-entry guard — count and log the skip so feed-format
            # changes surface in the source's health instead of silently
            # thinning the list.
            counts["malformed"] += 1
            sys.stderr.write(
                f"check-cfps-fetch: source B entry skipped ({type(exc).__name__}: {exc})\n"
            )
            continue

    return results, _finish_source("Source B", SOURCE_B_NAME, counts, warnings)


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------

VIRTUAL_KEYWORDS = {"online", "virtual", "remote", "hybrid"}

EXCLUDED_LOCATIONS = {
    "nigeria",
    "kenya",
    "south africa",
    "ghana",
    "ethiopia",
    "tanzania",
    "uganda",
    "rwanda",
}


def is_virtual(cfp: dict) -> bool:
    city = cfp.get("city", "").lower()
    name = cfp.get("name", "").lower()
    for kw in VIRTUAL_KEYWORDS:
        if kw in city or kw in name:
            return True
    return not city.strip()  # no location listed


def is_excluded_location(cfp: dict) -> bool:
    city = cfp.get("city", "").lower()
    for loc in EXCLUDED_LOCATIONS:
        if loc in city:
            return True
    return False


def load_travel_schedule(warnings: list) -> list:
    if not TRAVEL_PATH.exists():
        warnings.append("travel-schedule.json not found — skipping travel conflict check")
        return []
    try:
        with open(TRAVEL_PATH) as f:
            events = json.load(f)
        # Only trip events (no 'item-' in uid)
        trips = []
        for ev in events:
            if "item-" not in ev.get("uid", "") and ev.get("start") and ev.get("end"):
                try:
                    # `[:10]` slice handles both the date-only trip
                    # shape and the ISO-datetime item shape emitted by
                    # `refresh-travel-schedule.py` after
                    # `nanoclaw-admin#289`. Trips currently stay
                    # date-only, but the filter above only excludes
                    # `item-` UIDs — the slice keeps this loader safe
                    # against a future feed quirk that puts time on a
                    # trip-level VEVENT.
                    trips.append(
                        {
                            "start": date.fromisoformat(ev["start"][:10]),
                            "end": date.fromisoformat(ev["end"][:10]),
                        }
                    )
                except ValueError:
                    pass
        return trips
    except Exception as e:
        warnings.append(f"Failed to load travel-schedule.json: {e}")
        return []


def has_travel_conflict(cfp: dict, trips: list) -> bool:
    conf_date_str = cfp.get("conf_date", "")
    if not conf_date_str:
        return False
    try:
        # Treat conf_date as a single-day event for conflict check
        conf_start = date.fromisoformat(conf_date_str)
        # Assume 4-day conference if no end info
        from datetime import timedelta

        conf_end = conf_start + timedelta(days=4)
    except ValueError:
        return False
    for trip in trips:
        if conf_start <= trip["end"] and conf_end >= trip["start"]:
            return True
    return False


def load_cfp_state() -> dict:
    """Absent file = first run = empty state. Anything else that keeps the
    state from being read or parsed is a hard failure: failing open with
    `{}` would drop the sent/dismissed/remind filtering and resurface
    already-actioned CFPs as new candidates. Only FileNotFoundError means
    "first run" — an exists() pre-check would return False on e.g. a
    permission error and silently take the empty-state path."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        sys.stderr.write(
            f"check-cfps-fetch: cannot read {STATE_PATH}: "
            f"{type(e).__name__}: {e} — refusing to run without state "
            f"(sent/dismissed/remind filtering would be lost); restore or "
            f"repair the file and rerun\n"
        )
        sys.exit(1)
    if not isinstance(state, dict):
        sys.stderr.write(
            f"check-cfps-fetch: {STATE_PATH} root is "
            f"{type(state).__name__}, expected a JSON object — refusing to "
            f"run without usable state (sent/dismissed/remind filtering "
            f"would be lost); restore or repair the file and rerun\n"
        )
        sys.exit(1)
    return state


def apply_state_filter(cfp: dict, state: dict, today: date) -> bool:
    """Return True if this CFP should be shown (not filtered out by state)."""
    # Check blocked_prefixes — conference name patterns filtered indefinitely
    name_lower = cfp.get("name", "").lower()
    for prefix in state.get("_blocked_prefixes", []):
        if name_lower.startswith(prefix.lower()):
            return False

    slug = cfp.get("slug", "")
    entry = state.get(slug, {})
    status = entry.get("status", "")

    if status in ("sent", "dismissed"):
        return False

    if status == "remind":
        try:
            deadline = date.fromisoformat(cfp["deadline"])
            remind_days = entry.get("remind_before_days", 7)
            days_left = (deadline - today).days
            return days_left <= remind_days
        except (ValueError, KeyError):
            return True

    return True  # no state → show


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


def deduplicate(cfps: list) -> list:
    seen = {}
    result = []
    for cfp in cfps:
        key = cfp["name"].lower().strip()
        if key not in seen:
            seen[key] = True
            result.append(cfp)
    return result


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    warnings = []
    today = date.today()

    # Fetch
    source_a, health_a = fetch_developers_events(warnings)
    source_b, health_b = fetch_javaconferences(warnings)
    all_cfps = source_a + source_b
    sources = {SOURCE_A_NAME: health_a, SOURCE_B_NAME: health_b}
    feed_failure = all(h["status"] in FAILED_STATUSES for h in sources.values())

    # Gate on `feed_failure`: suggesting a web fallback for a run where every
    # feed failed technically would dress a format outage up as a normal empty
    # result — the exact conflation the per-source health exists to end. The
    # unreachable / all-malformed warnings already name what went wrong.
    # The wording says "no usable CFPs", not "both sources returned empty":
    # one source can be down while the other is validly empty, and claiming
    # emptiness for a source that never answered is its own small lie.
    if not all_cfps and not feed_failure:
        warnings.append("No usable CFPs from the primary sources — web search fallback needed")

    # Load supporting data
    trips = load_travel_schedule(warnings)
    state = load_cfp_state()

    # Enrich with slug and days_left
    for cfp in all_cfps:
        cfp["slug"] = make_slug(
            cfp["name"],
            conf_date=cfp.get("conf_date", ""),
            deadline=cfp.get("deadline", ""),
        )
        try:
            deadline = date.fromisoformat(cfp["deadline"])
            cfp["days_left"] = (deadline - today).days
        except ValueError:
            cfp["days_left"] = 9999

    # Filter — hard rules only; relevance judgment left to AI
    filtered = []
    for cfp in all_cfps:
        if is_virtual(cfp):
            continue
        if is_excluded_location(cfp):
            continue
        if cfp.get("days_left", 0) < 0:
            continue
        if has_travel_conflict(cfp, trips):
            continue
        if not apply_state_filter(cfp, state, today):
            continue
        filtered.append(cfp)

    # Deduplicate and sort by deadline
    filtered = deduplicate(filtered)
    filtered.sort(key=lambda c: c.get("deadline", "9999-99-99"))

    output = {
        "cfps": filtered,
        "warnings": warnings,
        "sources": sources,
        "feed_failure": feed_failure,
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
