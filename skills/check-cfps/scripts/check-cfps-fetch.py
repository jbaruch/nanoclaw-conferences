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
    "checked_at": "2026-03-29T05:00:00Z"
  }

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
    # Extract trailing year if present
    year_match = re.search(r"\b(20\d\d)\b", lower)
    if year_match:
        year = year_match.group(1)
    else:
        year = ""
        for candidate in (conf_date, deadline):
            parsed = parse_flexible_date(candidate)
            if parsed:
                year = str(parsed.year)
                break
        if not year:
            year = str(date.today().year)
    # Remove trailing year from base (will re-append)
    base = re.sub(r"\s*20\d\d\s*$", "", lower).strip()
    slug_base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    return f"{slug_base}-{year}"


# ---------------------------------------------------------------------------
# Source A: developers.events
# ---------------------------------------------------------------------------


def fetch_developers_events(warnings: list) -> list:
    url = "https://developers.events/all-cfps.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        warnings.append(f"Source A (developers.events) unreachable: {e}")
        return []

    if not isinstance(data, list):
        warnings.append("Source A: unexpected format (not a list)")
        return []

    now_ms = datetime.now(timezone.utc).timestamp() * 1000
    results = []

    for entry in data:
        try:
            until_ms = entry.get("untilDate", 0)
            if not until_ms or until_ms <= now_ms:
                continue

            # Feed timestamps are UTC epoch ms; convert in UTC explicitly.
            # A naive fromtimestamp uses the host timezone and shifts
            # deadlines by a day on non-UTC runners.
            deadline = datetime.fromtimestamp(until_ms / 1000, tz=timezone.utc).date()

            conf = entry.get("conf", {})
            name = conf.get("name", "").strip()
            if not name:
                continue

            cfp_url = entry.get("link", "") or conf.get("hyperlink", "")
            location = conf.get("location", "")

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
                    "source": "developers.events",
                }
            )
        except Exception as exc:
            # Per-entry guard: swallowing one bad entry is right, but
            # log so systematic upstream format changes become visible
            # instead of producing an empty output.
            sys.stderr.write(
                f"check-cfps-fetch: source A entry skipped ({type(exc).__name__}: {exc})\n"
            )
            continue

    return results


# ---------------------------------------------------------------------------
# Source B: javaconferences.org
# ---------------------------------------------------------------------------


def fetch_javaconferences(warnings: list) -> list:
    url = "https://javaconferences.org/conferences.json"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as e:
        warnings.append(f"Source B (javaconferences.org) unreachable: {e}")
        return []

    if not isinstance(data, list):
        warnings.append("Source B: unexpected format (not a list)")
        return []

    today = date.today()
    results = []

    for entry in data:
        try:
            cfp_link = entry.get("cfpLink", "").strip()
            if not cfp_link:
                continue

            cfp_end_str = entry.get("cfpEndDate", "")
            if not cfp_end_str:
                continue
            deadline = parse_flexible_date(cfp_end_str)
            if not deadline or deadline < today:
                continue

            name = entry.get("name", "").strip()
            if not name:
                continue

            location = entry.get("locationName", "")
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
                    "source": "javaconferences.org",
                }
            )
        except Exception as exc:
            # Per-entry guard — log skip so feed-format changes surface.
            sys.stderr.write(
                f"check-cfps-fetch: source B entry skipped ({type(exc).__name__}: {exc})\n"
            )
            continue

    return results


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
    source_a = fetch_developers_events(warnings)
    source_b = fetch_javaconferences(warnings)
    all_cfps = source_a + source_b

    if not all_cfps:
        warnings.append("Both primary sources returned empty — web search fallback needed")

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
        "checked_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
