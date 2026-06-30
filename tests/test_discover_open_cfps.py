"""Tests for skills/check-cfps/scripts/discover-open-cfps.py.

Step 2 new-candidate discovery must be produced by code, not improvised agent
Python — on 2026-06-29 an inline parse read a ~260 KB payload as "1 event" and
yielded 0 candidates (jbaruch/nanoclaw-conferences#9).

Contract under test:
  - a fixed fixture response yields the expected candidate COUNT (the #9
    acceptance) and the Step 2 candidate shape.
  - the host's default filter is applied: online events and user groups dropped.
  - slugs already in cfp-state.json are skipped; events with no usable cfpLink
    are skipped and counted.
  - GET {base}/open-cfps with the X-API-KEY header.
  - missing SESSIONIZE_SPEAKER_KEY -> exit 1; an API failure -> exit 1 (never a
    silent "0 new CFPs").
"""

from __future__ import annotations

import json

_BASE = "https://sessionize.com/api/universal"


def _event(
    slug,
    *,
    name=None,
    is_online=False,
    is_user_group=False,
    city="Berlin",
    end_utc="2026-09-30T23:59:59Z",
    start="2026-11-02",
):
    return {
        "name": name or slug.replace("-", " ").title(),
        "isOnline": is_online,
        "isUserGroup": is_user_group,
        "cfpLink": f"https://sessionize.com/{slug}",
        "cfpDates": {"endUtc": end_utc},
        "eventDates": {"start": start, "end": "2026-11-04"},
        "location": {"city": city, "country": "Germany", "full": f"{city}, Germany"},
    }


class _FakeResponse:
    def __init__(self, body):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def _patch_fetch(monkeypatch, *, events=None, raise_exc=None, capture=None):
    def _fake_urlopen(request, timeout=None):
        if capture is not None:
            capture.append((request.full_url, request.headers.get("X-api-key")))
        if raise_exc is not None:
            raise raise_exc
        return _FakeResponse(events)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


# --- pure parse (build_candidates) ----------------------------------------


def test_fixture_yields_expected_candidate_count(discover_open_cfps):
    module, _state = discover_open_cfps
    events = [_event(f"conf-{i}-2026") for i in range(5)]
    out = module.build_candidates(events, existing_slugs=set())
    assert out["counts"]["candidates_new"] == 5
    assert out["counts"]["events_seen"] == 5
    assert len(out["candidates"]) == 5


def test_candidate_shape_matches_step2(discover_open_cfps):
    module, _state = discover_open_cfps
    out = module.build_candidates([_event("devoxx-be-2026")], existing_slugs=set())
    (candidate,) = out["candidates"]
    assert candidate == {
        "name": "Devoxx Be 2026",
        "city": "Berlin",
        "conf_date": "2026-11-02",
        "deadline": "2026-09-30",
        "cfp_url": "https://sessionize.com/devoxx-be-2026",
        "slug": "devoxx-be-2026",
        "source": "sessionize-speaker-api",
    }


def test_online_and_user_group_events_filtered(discover_open_cfps):
    module, _state = discover_open_cfps
    events = [
        _event("keep-2026"),
        _event("online-2026", is_online=True),
        _event("usergroup-2026", is_user_group=True),
    ]
    out = module.build_candidates(events, existing_slugs=set())
    assert out["counts"]["after_filter"] == 1
    assert [c["slug"] for c in out["candidates"]] == ["keep-2026"]


def test_existing_slugs_skipped(discover_open_cfps):
    module, _state = discover_open_cfps
    events = [_event("new-2026"), _event("known-2026")]
    out = module.build_candidates(events, existing_slugs={"known-2026"})
    assert out["counts"]["skipped_existing"] == 1
    assert [c["slug"] for c in out["candidates"]] == ["new-2026"]


def test_event_without_cfplink_skipped_and_counted(discover_open_cfps):
    module, _state = discover_open_cfps
    bad = _event("x-2026")
    del bad["cfpLink"]
    out = module.build_candidates([bad, _event("good-2026")], existing_slugs=set())
    assert out["counts"]["skipped_no_slug"] == 1
    assert [c["slug"] for c in out["candidates"]] == ["good-2026"]


def test_missing_deadline_is_none(discover_open_cfps):
    module, _state = discover_open_cfps
    ev = _event("no-deadline-2026")
    ev["cfpDates"] = {}
    (candidate,) = module.build_candidates([ev], existing_slugs=set())["candidates"]
    assert candidate["deadline"] is None


def test_non_array_response_raises(discover_open_cfps):
    module, _state = discover_open_cfps
    try:
        module.build_candidates({"oops": "not a list"}, existing_slugs=set())
    except ValueError as exc:
        assert "expected a JSON array" in str(exc)
    else:
        raise AssertionError("expected ValueError on a non-array response")


# --- main() I/O + config ---------------------------------------------------


def test_main_fetches_with_x_api_key_and_skips_state(discover_open_cfps, monkeypatch, capsys):
    module, state_path = discover_open_cfps
    monkeypatch.setenv("SESSIONIZE_SPEAKER_KEY", "speaker-key")
    monkeypatch.setenv("SESSIONIZE_API_BASE", _BASE)
    state_path.write_text(
        json.dumps({"_blocked_prefixes": ["x"], "known-2026": {"status": "open"}}), encoding="utf-8"
    )
    captured: list = []
    _patch_fetch(monkeypatch, events=[_event("new-2026"), _event("known-2026")], capture=captured)

    code = module.main(["--state", str(state_path)])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    url, key = captured[0]
    assert url == f"{_BASE}/open-cfps"
    assert key == "speaker-key"
    # `_blocked_prefixes` is a config key, not a slug; only `known-2026` is skipped.
    assert [c["slug"] for c in out["candidates"]] == ["new-2026"]


def test_main_missing_key_exits_1(discover_open_cfps, monkeypatch, capsys):
    module, state_path = discover_open_cfps
    code = module.main(["--state", str(state_path)])
    assert code == 1
    assert "SESSIONIZE_SPEAKER_KEY" in capsys.readouterr().err


def test_main_api_failure_exits_1_not_silent_zero(discover_open_cfps, monkeypatch, capsys):
    module, state_path = discover_open_cfps
    monkeypatch.setenv("SESSIONIZE_SPEAKER_KEY", "speaker-key")
    _patch_fetch(monkeypatch, raise_exc=ConnectionError("boom"))

    code = module.main(["--state", str(state_path)])

    assert code == 1
    assert "fetch failed" in capsys.readouterr().err


def test_main_unreadable_state_exits_1(discover_open_cfps, monkeypatch, capsys):
    module, state_path = discover_open_cfps
    monkeypatch.setenv("SESSIONIZE_SPEAKER_KEY", "speaker-key")
    state_path.write_text("{ not json", encoding="utf-8")
    _patch_fetch(monkeypatch, events=[])

    code = module.main(["--state", str(state_path)])

    assert code == 1
    assert "cannot read" in capsys.readouterr().err
