"""Baseline tests for skills/check-cfps/scripts/check-cfps-fetch.py.

Locks down the documented contract per `coding-policy: testing-standards`:

  - Two upstream feeds (`developers.events`, `javaconferences.org`)
    are fetched via `urllib.request.urlopen`; per-source unreachability
    appends a warning and that source contributes zero entries
  - Both feeds empty appends a third "web search fallback needed"
    warning
  - Hard filters drop: virtual/online/remote/hybrid keywords (in name
    or city), missing-city entries, excluded-country locations, past
    deadlines, travel-window conflicts, and `cfp-state.json`
    `sent`/`dismissed` rows + `_blocked_prefixes`. `remind` rows are
    held until `days_left <= remind_before_days`
  - `EXCLUDED_LOCATIONS` is a case-insensitive substring match on
    `city`; `_blocked_prefixes` is a case-insensitive prefix match
    (`startswith`) on the lowercased name
  - Output: `{cfps, warnings, checked_at}` — `cfps` is deduplicated by
    case-insensitive name (first wins) and sorted ascending by
    `deadline`; `checked_at` is a UTC ISO-8601 string with `Z` suffix
  - Always exits 0 — the agent decides what to do with the output

Tests freeze `module.date` (today() AND timezone-deterministic
fromtimestamp()) and `module.datetime` (now()) so days_left math,
checked_at, and the source-A `until_ms > now_ms` pre-filter are
deterministic. `urllib.request.urlopen` is patched per-test to feed
canned source bodies or raise unreachability errors.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

_FROZEN_TODAY = date(2026, 4, 30)
_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

_DEV_EVENTS_URL = "https://developers.events/all-cfps.json"
_JAVA_CONFS_URL = "https://javaconferences.org/conferences.json"


def _make_frozen_date(real_date):
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

        @classmethod
        def fromtimestamp(cls, ts):
            # UTC-deterministic so source-A `until_ms` → deadline
            # arithmetic gives the same result on any CI runner
            # timezone, instead of leaking the host's local TZ into
            # the test outcome.
            return datetime.fromtimestamp(ts, tz=timezone.utc).date()

    return FrozenDate


def _make_frozen_datetime(real_datetime):
    class FrozenDateTime(real_datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return _FROZEN_NOW.replace(tzinfo=None)
            return _FROZEN_NOW.astimezone(tz)

    return FrozenDateTime


def _ms(d: date, hour: int = 12) -> int:
    """Convert a date to milliseconds since epoch at the given UTC hour.
    Noon UTC keeps the value safely inside the target day under the
    UTC-deterministic fromtimestamp override above."""
    return int(datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc).timestamp() * 1000)


def _src_a_entry(name, until_date, *, location="London, UK", conf_dates=None, link=""):
    """Source A (developers.events) entry shape — `untilDate` in ms,
    `conf.date` is a list of ms timestamps, `conf.location` is the
    city/country string the script's virtual + excluded-country
    filters consume."""
    return {
        "untilDate": _ms(until_date),
        "link": link or f"https://developers.events/cfp/{name.lower().replace(' ', '-')}",
        "conf": {
            "name": name,
            "location": location,
            "hyperlink": f"https://{name.lower().replace(' ', '-')}.example.test",
            "date": conf_dates or [],
        },
    }


def _src_b_entry(name, deadline_iso, *, location="Madrid, Spain", conf_date_iso="", cfp_link=""):
    """Source B (javaconferences.org) entry shape — `cfpEndDate` is an
    ISO date string parsed by `parse_flexible_date`; `locationName` is
    the venue string."""
    return {
        "name": name,
        "locationName": location,
        "date": conf_date_iso,
        "cfpEndDate": deadline_iso,
        "cfpLink": cfp_link or f"https://javaconferences.org/cfp/{name.lower().replace(' ', '-')}",
    }


class _FakeResponse:
    """Minimal urllib.request response stand-in: supports the context
    manager protocol and `.read()`. Bodies are encoded UTF-8 bytes to
    match `urllib.request.urlopen(...).read()`."""

    def __init__(self, body):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return False

    def read(self):
        return self._body


def _patch_urlopen(monkeypatch, *, source_a=None, source_b=None):
    """Patch `urllib.request.urlopen` to dispatch per URL substring.

    `source_a` / `source_b` are either Python objects (json-serialized),
    raw bytes/str (returned as-is — useful for the not-a-list and
    malformed-JSON paths), or Exception instances (raised on call —
    drives the "Source X unreachable" branch)."""

    payloads = {}
    if source_a is not None:
        payloads[_DEV_EVENTS_URL] = source_a
    if source_b is not None:
        payloads[_JAVA_CONFS_URL] = source_b

    def _fake_urlopen(url, timeout=None):
        target = url if isinstance(url, str) else getattr(url, "full_url", str(url))
        for needle, payload in payloads.items():
            if needle in target:
                if isinstance(payload, Exception):
                    raise payload
                if isinstance(payload, (bytes, str)):
                    return _FakeResponse(payload)
                return _FakeResponse(json.dumps(payload))
        raise AssertionError(f"unexpected URL fetched: {target!r}")

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def _run(module, monkeypatch, capsys):
    """Invoke main() with frozen date/datetime and captured stdout."""
    monkeypatch.setattr("sys.argv", ["check-cfps-fetch.py"])
    monkeypatch.setattr(module, "date", _make_frozen_date(date))
    monkeypatch.setattr(module, "datetime", _make_frozen_datetime(datetime))
    code = 0
    try:
        result = module.main()
        code = 0 if result is None else int(result)
    except SystemExit as exc:
        code = 0 if exc.code is None else int(exc.code)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# Happy path + source merging
# ---------------------------------------------------------------------------


def test_both_sources_merged_and_sorted(check_cfps_fetch, monkeypatch, capsys):
    """Source A + Source B both deliver one CFP each → both ship,
    sorted ascending by deadline; no warnings since both feeds
    succeeded and the travel file is present (empty)."""
    module, _, travel_path = check_cfps_fetch
    travel_path.write_text("[]")
    src_a = [_src_a_entry("AlphaConf 2026", _FROZEN_TODAY + timedelta(days=20))]
    src_b = [_src_b_entry("BravoConf 2026", (_FROZEN_TODAY + timedelta(days=10)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=src_a, source_b=src_b)

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 0
    assert err == ""
    payload = json.loads(out)
    names = [c["name"] for c in payload["cfps"]]
    assert names == ["BravoConf 2026", "AlphaConf 2026"]
    assert payload["warnings"] == []


def test_checked_at_is_utc_iso_with_z(check_cfps_fetch, monkeypatch, capsys):
    """`checked_at` field — UTC ISO-8601 with literal `Z` suffix per
    the docstring."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=[], source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["checked_at"] == "2026-04-30T12:00:00Z"


# ---------------------------------------------------------------------------
# Source unreachability + format guards
# ---------------------------------------------------------------------------


def test_source_a_unreachable_appends_warning_keeps_b(check_cfps_fetch, monkeypatch, capsys):
    """Source A raises → its warning is appended; Source B's CFP still
    ships."""
    module, _, _ = check_cfps_fetch
    src_b = [_src_b_entry("OnlyB 2026", (_FROZEN_TODAY + timedelta(days=14)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=ConnectionError("boom"), source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert [c["name"] for c in payload["cfps"]] == ["OnlyB 2026"]
    assert any(
        "Source A (developers.events) unreachable" in w for w in payload["warnings"]
    ), payload["warnings"]
    # No "both empty" warning — B delivered content.
    assert not any("web search fallback needed" in w for w in payload["warnings"])


def test_both_sources_empty_emits_fallback_warning(check_cfps_fetch, monkeypatch, capsys):
    """Both feeds reachable but yield zero entries → no per-source
    warning, but the explicit `web search fallback needed` warning
    fires so the caller knows the deterministic path produced
    nothing."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=[], source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert payload["cfps"] == []
    assert any("web search fallback needed" in w for w in payload["warnings"])
    assert not any("unreachable" in w for w in payload["warnings"])


def test_source_a_non_list_payload_warns(check_cfps_fetch, monkeypatch, capsys):
    """Source A returning a JSON object instead of a list → explicit
    "unexpected format" warning, no entries from A."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a={"oops": "not a list"}, source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert any("Source A: unexpected format" in w for w in payload["warnings"]), payload["warnings"]


# ---------------------------------------------------------------------------
# Hard filters
# ---------------------------------------------------------------------------


def test_virtual_keyword_in_name_filtered(check_cfps_fetch, monkeypatch, capsys):
    """Conference name containing a `VIRTUAL_KEYWORDS` token (e.g.
    "Online") drops it before output."""
    module, _, _ = check_cfps_fetch
    src_b = [
        _src_b_entry("InPersonConf 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat()),
        _src_b_entry("Online Summit 2026", (_FROZEN_TODAY + timedelta(days=14)).isoformat()),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["InPersonConf 2026"]


def test_missing_city_treated_as_virtual(check_cfps_fetch, monkeypatch, capsys):
    """Empty `city` → `is_virtual` returns True (no location listed
    branch)."""
    module, _, _ = check_cfps_fetch
    src_b = [_src_b_entry("HasCity 2026", (_FROZEN_TODAY + timedelta(days=8)).isoformat())]
    src_b.append(
        _src_b_entry("NoCity 2026", (_FROZEN_TODAY + timedelta(days=9)).isoformat(), location="")
    )
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["HasCity 2026"]


def test_excluded_country_filtered(check_cfps_fetch, monkeypatch, capsys):
    """`EXCLUDED_LOCATIONS` substring match on city — case
    insensitive."""
    module, _, _ = check_cfps_fetch
    src_b = [
        _src_b_entry(
            "LagosConf 2026",
            (_FROZEN_TODAY + timedelta(days=11)).isoformat(),
            location="Lagos, Nigeria",
        ),
        _src_b_entry(
            "BarcelonaConf 2026",
            (_FROZEN_TODAY + timedelta(days=13)).isoformat(),
            location="Barcelona, Spain",
        ),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["BarcelonaConf 2026"]


def test_travel_conflict_filtered(check_cfps_fetch, monkeypatch, capsys):
    """`travel-schedule.json` overlap with the (4-day) conference
    window drops the CFP. `item-` UIDs must be ignored — they're
    individual calendar items, not trips."""
    module, _, travel_path = check_cfps_fetch
    conflict_start = _FROZEN_TODAY + timedelta(days=20)
    travel_path.write_text(
        json.dumps(
            [
                # Overlaps the 4-day window starting at conflict_start.
                {
                    "uid": "trip-1",
                    "start": conflict_start.isoformat(),
                    "end": (conflict_start + timedelta(days=2)).isoformat(),
                },
                # Calendar item — must be ignored even if it overlaps.
                {
                    "uid": "item-99",
                    "start": (_FROZEN_TODAY + timedelta(days=40)).isoformat(),
                    "end": (_FROZEN_TODAY + timedelta(days=41)).isoformat(),
                },
            ]
        )
    )

    src_b = [
        _src_b_entry(
            "Conflict 2026",
            (_FROZEN_TODAY + timedelta(days=25)).isoformat(),
            conf_date_iso=conflict_start.isoformat(),
        ),
        _src_b_entry(
            "Clear 2026",
            (_FROZEN_TODAY + timedelta(days=42)).isoformat(),
            conf_date_iso=(_FROZEN_TODAY + timedelta(days=40)).isoformat(),
        ),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["Clear 2026"]


def test_missing_travel_file_emits_warning(check_cfps_fetch, monkeypatch, capsys):
    """`travel-schedule.json` absent → explicit "skipping travel
    conflict check" warning so callers know the conflict filter
    didn't run."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=[], source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    warnings = json.loads(out)["warnings"]
    assert any("travel-schedule.json not found" in w for w in warnings), warnings


# ---------------------------------------------------------------------------
# cfp-state filters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["sent", "dismissed"])
def test_cfp_state_terminal_status_filters(check_cfps_fetch, monkeypatch, capsys, status):
    """`status: sent` and `status: dismissed` are terminal — drop the
    CFP without re-emitting it."""
    module, state_path, _ = check_cfps_fetch
    # Slug for "FinishedConf 2026" — `make_slug` strips internal
    # whitespace/punctuation and re-appends the year.
    state_path.write_text(json.dumps({"finishedconf-2026": {"status": status}}))

    src_b = [
        _src_b_entry("FinishedConf 2026", (_FROZEN_TODAY + timedelta(days=15)).isoformat()),
        _src_b_entry("FreshConf 2026", (_FROZEN_TODAY + timedelta(days=18)).isoformat()),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["FreshConf 2026"]


def test_cfp_state_remind_holds_until_window(check_cfps_fetch, monkeypatch, capsys):
    """`status: remind` with `remind_before_days: 7` — drops while
    days_left > 7, ships when days_left <= 7."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_text(
        json.dumps(
            {
                "earlyconf-2026": {"status": "remind", "remind_before_days": 7},
                "imminentconf-2026": {"status": "remind", "remind_before_days": 7},
            }
        )
    )
    src_b = [
        # 30 days out — outside the 7-day remind window, must drop.
        _src_b_entry("EarlyConf 2026", (_FROZEN_TODAY + timedelta(days=30)).isoformat()),
        # 5 days out — inside the window, must ship.
        _src_b_entry("ImminentConf 2026", (_FROZEN_TODAY + timedelta(days=5)).isoformat()),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["ImminentConf 2026"]


def test_blocked_prefix_filters_case_insensitive(check_cfps_fetch, monkeypatch, capsys):
    """`_blocked_prefixes` matches the start of a lowercased name —
    case-insensitive prefix (`startswith`) match per
    `apply_state_filter`."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_text(json.dumps({"_blocked_prefixes": ["BlockMe"]}))

    src_b = [
        _src_b_entry("BlockMe Summit 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat()),
        _src_b_entry("Allowed 2026", (_FROZEN_TODAY + timedelta(days=14)).isoformat()),
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    names = [c["name"] for c in json.loads(out)["cfps"]]
    assert names == ["Allowed 2026"]


def test_corrupt_state_file_emits_warning_and_continues(check_cfps_fetch, monkeypatch, capsys):
    """Malformed `cfp-state.json` → warning, no state filtering applied
    (everything passes through), pipeline still produces output."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_text("{not json")

    src_b = [_src_b_entry("StillShips 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)
    assert any("Failed to load cfp-state.json" in w for w in payload["warnings"])
    assert [c["name"] for c in payload["cfps"]] == ["StillShips 2026"]


# ---------------------------------------------------------------------------
# Dedup + slug
# ---------------------------------------------------------------------------


def test_deduplication_keeps_first_seen(check_cfps_fetch, monkeypatch, capsys):
    """Same conference name appearing in both sources → only the first
    (Source A is fetched first) survives. Comparison is
    case-insensitive on `name`."""
    module, _, _ = check_cfps_fetch
    src_a = [_src_a_entry("DupConf 2026", _FROZEN_TODAY + timedelta(days=20))]
    src_b = [_src_b_entry("dupconf 2026", (_FROZEN_TODAY + timedelta(days=22)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=src_a, source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert len(cfps) == 1
    assert cfps[0]["source"] == "developers.events"


def test_slug_includes_year_for_state_lookup(check_cfps_fetch, monkeypatch, capsys):
    """`make_slug` slugifies the lowercased name and appends the
    embedded year — required so `cfp-state.json` keys can target a
    specific year of a recurring conference."""
    module, _, _ = check_cfps_fetch
    src_b = [_src_b_entry("PyConf 2026", (_FROZEN_TODAY + timedelta(days=15)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert cfps[0]["slug"] == "pyconf-2026"
