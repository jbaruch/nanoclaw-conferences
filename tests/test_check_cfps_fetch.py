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
  - Exits 0 on success; exits 1 (fail-closed) when `cfp-state.json`
    exists but cannot be read or parsed, so terminal-state filtering
    is never silently skipped

Tests freeze `module.date` (today()) and `module.datetime` (now()) so
days_left math, checked_at, and the source-A `until_ms > now_ms`
pre-filter are deterministic. Epoch→date conversion is NOT masked in
the fixtures — the script converts with an explicit tz=timezone.utc,
and a dedicated regression test runs it under a non-UTC host zone.
`urllib.request.urlopen` is patched per-test to feed canned source
bodies or raise unreachability errors.
"""

import json
import os
import time
from datetime import date, datetime, timedelta, timezone

import pytest

_FROZEN_TODAY = date(2026, 4, 30)
_FROZEN_NOW = datetime(2026, 4, 30, 12, 0, 0, tzinfo=timezone.utc)

_DEV_EVENTS_URL = "https://developers.events/all-cfps.json"
_JAVA_CONFS_URL = "https://javaconferences.org/conferences.json"


def _make_frozen_date(real_date):
    # No fromtimestamp override here: production code converts epoch
    # timestamps with an explicit tz=timezone.utc, so tests exercise the
    # real conversion path on any runner timezone. Masking it in the
    # fixture is what let a host-TZ-dependent conversion ship (#36).
    class FrozenDate(real_date):
        @classmethod
        def today(cls):
            return _FROZEN_TODAY

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
    script's explicit-UTC epoch conversion."""
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
    assert any("Source A (developers.events) unreachable" in w for w in payload["warnings"]), (
        payload["warnings"]
    )
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


def test_corrupt_state_file_aborts(check_cfps_fetch, monkeypatch, capsys):
    """Malformed `cfp-state.json` → hard failure (exit 1, stderr diagnostic,
    no CFP output). Failing open with `{}` would drop sent/dismissed/remind
    filtering and resurface already-actioned CFPs as new."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_text("{not json")

    src_b = [_src_b_entry("StillShips 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert "cannot read" in err
    assert str(state_path) in err
    assert out == ""


def test_non_object_state_root_aborts(check_cfps_fetch, monkeypatch, capsys):
    """Syntactically valid JSON with a non-object root (e.g. `[]`) is
    invalid state — abort with a diagnostic, not an AttributeError
    traceback later in apply_state_filter."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_text("[]")

    src_b = [_src_b_entry("StillShips 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert "expected a JSON object" in err
    assert str(state_path) in err
    assert out == ""


def test_invalid_utf8_state_file_aborts(check_cfps_fetch, monkeypatch, capsys):
    """A state file that exists but is not valid UTF-8 → same hard failure
    as malformed JSON, not a traceback and not fail-open."""
    module, state_path, _ = check_cfps_fetch
    state_path.write_bytes(b"\xff\xfe{}")

    src_b = [_src_b_entry("StillShips 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    code, out, err = _run(module, monkeypatch, capsys)
    assert code == 1
    assert "cannot read" in err
    assert out == ""


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


def test_yearless_name_slug_year_from_conf_date(check_cfps_fetch, monkeypatch, capsys):
    """A feed name without a year keys under the conference's own year,
    not the wall clock: a next-January conference fetched in the prior
    calendar year must not get this year's slug (it would miss its
    existing cfp-state row and dodge sent/dismissed filtering)."""
    module, _, _ = check_cfps_fetch
    src_b = [
        _src_b_entry(
            "Devoxx Atlantis",
            (_FROZEN_TODAY + timedelta(days=30)).isoformat(),
            conf_date_iso="2027-01-20",
        )
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert cfps[0]["slug"] == "devoxx-atlantis-2027"


def test_yearless_name_slug_year_from_deadline_when_no_conf_date(
    check_cfps_fetch, monkeypatch, capsys
):
    """Without a conf_date, the deadline year beats the current year:
    a CFP closing next January keys under next year, not under
    today's calendar year."""
    module, _, _ = check_cfps_fetch
    src_b = [_src_b_entry("Winter Summit", "2027-01-10")]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert cfps[0]["slug"] == "winter-summit-2027"


def test_embedded_name_year_beats_conf_date_year(check_cfps_fetch, monkeypatch, capsys):
    """A year embedded in the feed name wins over conf_date — the name
    is the feed's own statement of which edition this is."""
    module, _, _ = check_cfps_fetch
    src_b = [
        _src_b_entry(
            "PyConf 2026",
            (_FROZEN_TODAY + timedelta(days=15)).isoformat(),
            conf_date_iso="2027-02-01",
        )
    ]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert cfps[0]["slug"] == "pyconf-2026"


def test_mid_name_year_not_duplicated_in_slug(check_cfps_fetch, monkeypatch, capsys):
    """A year in the middle of the name is used as the slug year and
    removed from the base — not left in place to be duplicated
    ("kubecon-2026-eu-2026")."""
    module, _, _ = check_cfps_fetch
    src_b = [_src_b_entry("KubeCon 2026 EU", (_FROZEN_TODAY + timedelta(days=15)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[], source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    cfps = json.loads(out)["cfps"]
    assert cfps[0]["slug"] == "kubecon-eu-2026"


@pytest.mark.skipif(
    not hasattr(time, "tzset"),
    reason="time.tzset() unavailable on this platform (Windows) — cannot shift the host TZ",
)
def test_source_a_epoch_conversion_is_utc(check_cfps_fetch, monkeypatch, capsys):
    """Source-A epoch-ms fields must convert in UTC regardless of the
    host timezone. A deadline at 23:00 UTC is already 'tomorrow' in
    UTC+14 — a naive fromtimestamp would shift both `deadline` and
    `conf_date` a day late there. No fixture masking: this exercises
    the script's real conversion under a shifted TZ."""
    module, _, _ = check_cfps_fetch
    deadline_day = _FROZEN_TODAY + timedelta(days=15)
    conf_day = _FROZEN_TODAY + timedelta(days=40)
    entry = _src_a_entry("TzConf 2026", deadline_day, conf_dates=[_ms(conf_day, hour=23)])
    entry["untilDate"] = _ms(deadline_day, hour=23)
    _patch_urlopen(monkeypatch, source_a=[entry], source_b=[])

    old_tz = os.environ.get("TZ")
    # POSIX TZ string, not a zoneinfo name: "GMT-14" means UTC+14 (POSIX
    # inverts the sign) and needs no tzdata entry, so the shift works on
    # minimal images too.
    os.environ["TZ"] = "GMT-14"
    time.tzset()
    try:
        # Prove the shift took effect — a no-op tzset would leave the
        # process on UTC and make this regression test vacuous.
        assert time.timezone == -14 * 3600, "TZ shift did not take effect"
        _, out, _ = _run(module, monkeypatch, capsys)
        cfps = json.loads(out)["cfps"]
        assert cfps[0]["deadline"] == deadline_day.isoformat()
        assert cfps[0]["conf_date"] == conf_day.isoformat()
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()


# ---------------------------------------------------------------------------
# Per-source feed health (jbaruch/nanoclaw-conferences#78)
# ---------------------------------------------------------------------------


def _health(payload, source):
    return payload["sources"][source]


def test_classify_source_predicate(check_cfps_fetch):
    """The status predicate itself: emptiness, all-filtered, all-malformed,
    partial, and clean are five distinct outcomes."""
    module, _, _ = check_cfps_fetch
    assert module.classify_source(0, 0, 0) == "empty"
    assert module.classify_source(5, 0, 0) == "filtered"
    assert module.classify_source(5, 0, 5) == "all_malformed"
    assert module.classify_source(5, 0, 2) == "all_malformed"
    assert module.classify_source(5, 3, 2) == "partial"
    assert module.classify_source(5, 5, 0) == "ok"


def test_clean_feeds_report_ok_and_no_failure(check_cfps_fetch, monkeypatch, capsys):
    """Both feeds deliver usable records → per-source `ok`, honest counts,
    and `feed_failure` false."""
    module, _, travel_path = check_cfps_fetch
    travel_path.write_text("[]")
    src_a = [_src_a_entry("AlphaConf 2026", _FROZEN_TODAY + timedelta(days=20))]
    src_b = [_src_b_entry("BravoConf 2026", (_FROZEN_TODAY + timedelta(days=10)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=src_a, source_b=src_b)

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is False
    assert _health(payload, "developers.events") == {
        "status": "ok",
        "records_received": 1,
        "records_usable": 1,
        "records_malformed": 0,
        "records_filtered": 0,
    }
    assert _health(payload, "javaconferences.org")["status"] == "ok"


def test_all_malformed_source_is_distinguished_from_empty(check_cfps_fetch, monkeypatch, capsys):
    """The regression: every Source A record malformed used to look exactly
    like a valid empty feed — empty `cfps`, no warning. It now reports
    `all_malformed` with a source-level warning."""
    module, _, travel_path = check_cfps_fetch
    travel_path.write_text("[]")
    nameless = _src_a_entry("Placeholder 2026", _FROZEN_TODAY + timedelta(days=20))
    nameless["conf"]["name"] = ""
    no_deadline = _src_a_entry("Other 2026", _FROZEN_TODAY + timedelta(days=20))
    no_deadline["untilDate"] = None
    src_b = [_src_b_entry("BravoConf 2026", (_FROZEN_TODAY + timedelta(days=10)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=[nameless, no_deadline], source_b=src_b)

    _, out, err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "developers.events") == {
        "status": "all_malformed",
        "records_received": 2,
        "records_usable": 0,
        "records_malformed": 2,
        "records_filtered": 0,
    }
    assert any("all 2 records unusable (2 malformed)" in w for w in payload["warnings"]), payload[
        "warnings"
    ]
    assert "no conf.name" in err
    # Source B still delivered, so this is not a whole-run failure.
    assert payload["feed_failure"] is False
    assert [c["name"] for c in payload["cfps"]] == ["BravoConf 2026"]


def test_valid_empty_feeds_are_not_a_failure(check_cfps_fetch, monkeypatch, capsys):
    """Both feeds reachable and genuinely empty → `empty` status on each and
    `feed_failure` false: nothing published is not a technical failure."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=[], source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is False
    assert _health(payload, "developers.events")["status"] == "empty"
    assert _health(payload, "javaconferences.org")["status"] == "empty"
    assert _health(payload, "javaconferences.org")["records_malformed"] == 0


def test_fully_filtered_feed_is_not_malformed(check_cfps_fetch, monkeypatch, capsys):
    """Source B entries dropped by the feed's own normal rules — a closed CFP
    and a conference with no CFP link — count as filtered, not malformed."""
    module, _, _ = check_cfps_fetch
    closed = _src_b_entry("ClosedConf 2026", (_FROZEN_TODAY - timedelta(days=1)).isoformat())
    no_cfp = _src_b_entry("NoCfpConf 2026", (_FROZEN_TODAY + timedelta(days=30)).isoformat())
    no_cfp["cfpLink"] = ""
    _patch_urlopen(monkeypatch, source_a=[], source_b=[closed, no_cfp])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "javaconferences.org") == {
        "status": "filtered",
        "records_received": 2,
        "records_usable": 0,
        "records_malformed": 0,
        "records_filtered": 2,
    }
    assert payload["feed_failure"] is False


def test_partially_usable_feed_keeps_its_usable_records(check_cfps_fetch, monkeypatch, capsys):
    """One good record plus one whose date the parser cannot read → `partial`:
    the usable record ships and the drift is still counted."""
    module, _, _ = check_cfps_fetch
    good = _src_b_entry("GoodConf 2026", (_FROZEN_TODAY + timedelta(days=12)).isoformat())
    broken = _src_b_entry("BrokenConf 2026", "next tuesday-ish")
    _patch_urlopen(monkeypatch, source_a=[], source_b=[good, broken])

    _, out, err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "javaconferences.org") == {
        "status": "partial",
        "records_received": 2,
        "records_usable": 1,
        "records_malformed": 1,
        "records_filtered": 0,
    }
    assert [c["name"] for c in payload["cfps"]] == ["GoodConf 2026"]
    assert "cfpEndDate unparseable" in err
    assert payload["feed_failure"] is False


def test_null_cfp_link_is_filtered_not_malformed(check_cfps_fetch, monkeypatch, capsys):
    """A JSON `null` cfpLink is the same "no CFP open" case as a missing one.
    Stripping it blind would count the record malformed and let a healthy feed
    of link-less conferences escalate to `feed_failure`."""
    module, _, _ = check_cfps_fetch
    null_link: dict = _src_b_entry(
        "NoLinkConf 2026", (_FROZEN_TODAY + timedelta(days=30)).isoformat()
    )
    null_link["cfpLink"] = None
    bad_link: dict = _src_b_entry(
        "BadLinkConf 2026", (_FROZEN_TODAY + timedelta(days=30)).isoformat()
    )
    bad_link["cfpLink"] = 7
    _patch_urlopen(monkeypatch, source_a=[], source_b=[null_link, bad_link])

    _, out, err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "javaconferences.org") == {
        "status": "all_malformed",
        "records_received": 2,
        "records_usable": 0,
        "records_malformed": 1,
        "records_filtered": 1,
    }
    assert "non-string cfpLink" in err


def test_all_null_cfp_links_stay_a_filtered_feed(check_cfps_fetch, monkeypatch, capsys):
    """The escalation the previous test guards against, end to end: every
    entry link-less via `null` is a `filtered` feed, never `feed_failure`."""
    module, _, _ = check_cfps_fetch
    entries = []
    for name in ("OneConf 2026", "TwoConf 2026"):
        entry: dict = _src_b_entry(name, (_FROZEN_TODAY + timedelta(days=30)).isoformat())
        entry["cfpLink"] = None
        entries.append(entry)
    _patch_urlopen(monkeypatch, source_a=[], source_b=entries)

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "javaconferences.org")["status"] == "filtered"
    assert _health(payload, "javaconferences.org")["records_filtered"] == 2
    assert payload["feed_failure"] is False


def test_undecodable_by_recursion_is_a_format_failure(check_cfps_fetch, monkeypatch, capsys):
    """A decoder failure is a decoder failure whatever it raises: a document
    nested past the interpreter's limit yields `malformed_feed`, not an
    escaping traceback that leaves the caller no health record at all."""
    module, _, _ = check_cfps_fetch
    deeply_nested = "[" * 20000 + "]" * 20000
    _patch_urlopen(monkeypatch, source_a=deeply_nested, source_b=[])

    code, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert code == 0
    assert _health(payload, "developers.events")["status"] == "malformed_feed"
    assert any("is not valid JSON" in w for w in payload["warnings"]), payload["warnings"]


def test_oversized_integer_literal_is_a_format_failure(check_cfps_fetch, monkeypatch, capsys):
    """An integer literal past the interpreter's digit limit raises a bare
    ValueError, not a JSONDecodeError. Naming only the subclass let it escape
    and abort the run — taking the healthy source's results with it."""
    module, _, _ = check_cfps_fetch
    huge_int_body = "[" + "9" * 5000 + "]"
    src_b = [_src_b_entry("BravoConf 2026", (_FROZEN_TODAY + timedelta(days=10)).isoformat())]
    _patch_urlopen(monkeypatch, source_a=huge_int_body, source_b=src_b)

    code, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert code == 0
    assert _health(payload, "developers.events")["status"] == "malformed_feed"
    assert any("is not valid JSON" in w for w in payload["warnings"]), payload["warnings"]
    # Graceful fallback: the other source's results survive.
    assert [c["name"] for c in payload["cfps"]] == ["BravoConf 2026"]
    assert payload["feed_failure"] is False


def test_zero_epoch_deadline_is_filtered_not_malformed(check_cfps_fetch, monkeypatch, capsys):
    """`untilDate: 0` is a numeric timestamp (1970) and so a closed CFP.
    Testing truthiness instead of presence counted it malformed, which could
    push an otherwise filtered source to `all_malformed` and `feed_failure`."""
    module, _, _ = check_cfps_fetch
    zero: dict = _src_a_entry("EpochConf 2026", _FROZEN_TODAY + timedelta(days=20))
    zero["untilDate"] = 0
    missing: dict = _src_a_entry("NoDeadlineConf 2026", _FROZEN_TODAY + timedelta(days=20))
    del missing["untilDate"]
    _patch_urlopen(monkeypatch, source_a=[zero, missing], source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "developers.events") == {
        "status": "all_malformed",
        "records_received": 2,
        "records_usable": 0,
        "records_malformed": 1,
        "records_filtered": 1,
    }


def test_every_source_failed_sets_feed_failure(check_cfps_fetch, monkeypatch, capsys):
    """Source A unreachable and Source B all-malformed → `feed_failure` true,
    the single branch point the scheduled technical-failure path reads."""
    module, _, _ = check_cfps_fetch
    broken = _src_b_entry("BrokenConf 2026", "")
    _patch_urlopen(monkeypatch, source_a=ConnectionError("boom"), source_b=[broken])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is True
    assert _health(payload, "developers.events")["status"] == "unreachable"
    assert _health(payload, "javaconferences.org")["status"] == "all_malformed"


def test_unreachable_plus_valid_empty_is_not_feed_failure(check_cfps_fetch, monkeypatch, capsys):
    """One source down, the other validly empty: not every source failed for a
    technical reason, so the run is not a technical failure."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=ConnectionError("boom"), source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is False
    assert _health(payload, "developers.events")["status"] == "unreachable"
    assert _health(payload, "javaconferences.org")["status"] == "empty"


def test_non_list_roots_report_malformed_feed(check_cfps_fetch, monkeypatch, capsys):
    """A JSON object where a list belongs is a feed-level format failure on
    both sources, and every source failing sets `feed_failure`."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a='{"cfps": []}', source_b='{"conferences": []}')

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is True
    assert _health(payload, "developers.events")["status"] == "malformed_feed"
    assert _health(payload, "javaconferences.org")["status"] == "malformed_feed"


def test_undecodable_body_is_a_format_failure_not_unreachable(
    check_cfps_fetch, monkeypatch, capsys
):
    """A response that arrived but is not JSON is `malformed_feed`. Folding the
    parse into the transport `except` labelled it `unreachable`, which is the
    conflation the health contract exists to remove."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a="<html>502 Bad Gateway</html>", source_b="not json")

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert _health(payload, "developers.events")["status"] == "malformed_feed"
    assert _health(payload, "javaconferences.org")["status"] == "malformed_feed"
    assert any("is not valid JSON" in w for w in payload["warnings"]), payload["warnings"]
    assert payload["feed_failure"] is True


def test_non_string_location_is_counted_not_fatal(check_cfps_fetch, monkeypatch, capsys):
    """`location` is never strip()ed, so a non-string used to escape the
    per-entry guard and abort the whole run inside is_virtual()'s .lower().
    It is now a counted malformed record on both sources."""
    module, _, _ = check_cfps_fetch
    bad_a = _src_a_entry("NullLocA 2026", _FROZEN_TODAY + timedelta(days=20))
    bad_a["conf"]["location"] = None
    good_a = _src_a_entry("GoodA 2026", _FROZEN_TODAY + timedelta(days=25))
    # Annotated loosely on purpose: the point of the fixture is a field whose
    # type the feed got wrong.
    bad_b: dict = _src_b_entry("NullLocB 2026", (_FROZEN_TODAY + timedelta(days=10)).isoformat())
    bad_b["locationName"] = 42
    _patch_urlopen(monkeypatch, source_a=[bad_a, good_a], source_b=[bad_b])

    code, out, err = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert code == 0
    assert [c["name"] for c in payload["cfps"]] == ["GoodA 2026"]
    assert _health(payload, "developers.events")["status"] == "partial"
    assert _health(payload, "developers.events")["records_malformed"] == 1
    assert _health(payload, "javaconferences.org")["status"] == "all_malformed"
    assert "non-string location" in err
    assert "non-string locationName" in err


def test_one_source_down_one_validly_empty_does_not_claim_emptiness(
    check_cfps_fetch, monkeypatch, capsys
):
    """The mixed case: a source that never answered is not a source that
    returned empty. The fallback warning still fires (no usable CFPs, and this
    is not a technical failure) but does not claim both feeds were empty."""
    module, _, _ = check_cfps_fetch
    _patch_urlopen(monkeypatch, source_a=ConnectionError("boom"), source_b=[])

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is False
    assert any("No usable CFPs from the primary sources" in w for w in payload["warnings"])
    assert not any("Both primary sources returned empty" in w for w in payload["warnings"])


def test_feed_failure_suppresses_the_web_fallback_warning(check_cfps_fetch, monkeypatch, capsys):
    """Every feed failed technically → no "web search fallback needed": that
    wording would make a format outage read as a normal empty result. The
    per-source warnings still name what actually broke."""
    module, _, _ = check_cfps_fetch
    nameless = _src_a_entry("Placeholder 2026", _FROZEN_TODAY + timedelta(days=20))
    nameless["conf"]["name"] = ""
    _patch_urlopen(monkeypatch, source_a=[nameless], source_b=ConnectionError("boom"))

    _, out, _ = _run(module, monkeypatch, capsys)
    payload = json.loads(out)

    assert payload["feed_failure"] is True
    assert payload["cfps"] == []
    assert not any("web search fallback needed" in w for w in payload["warnings"])
    assert any("all 1 records unusable" in w for w in payload["warnings"]), payload["warnings"]
    assert any("Source B (javaconferences.org) unreachable" in w for w in payload["warnings"])
