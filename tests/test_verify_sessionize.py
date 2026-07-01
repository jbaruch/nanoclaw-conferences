"""Tests for skills/check-cfps/scripts/verify-sessionize.py.

The driver collapses prepare -> per-slug live events fetch -> apply into one
step so the Sessionize round-trip can't be skipped by the agent
(jbaruch/nanoclaw-conferences#7), and writes the verification-evidence marker
the stamp gate reads (jbaruch/nanoclaw-conferences#8).

Contract under test (matches the host's universal API the tool used to call):
  - per-slug GET {base}/event?slug=..., X-API-KEY header, raw event normalized
    in-script via the ported normalizeSessionizeEvent.
  - a per-slug failure isolates to {slug, error} -> apply marks it
    verify_failed; it never sinks the rest of the cohort.
  - NO fabricated verdicts: a total outage makes every entry verify_failed,
    evidence shows nothing resolved, exit 0 (the verify_failed decisions are the
    product Step 8 persists).
  - empty Sessionize slug set: no call, slugs_expected=0.
  - missing SESSIONIZE_EVENT_API_KEY with slugs -> exit 1; malformed stdin -> 1.
"""

from __future__ import annotations

import io
import json

import pytest

_BASE = "https://sessionize.com/api/universal"

# A raw Sessionize universal-event payload (the shape the API returns, pinned
# from the host's normalizeSessionizeEvent fixture). Far-future CFP end keeps
# cfp_open independent of the wall clock.
_RAW_OPEN_EVENT = {
    "name": "Devoxx Belgium 2026",
    "cfpDates": {
        "startUtc": "2026-01-01T00:00:00Z",
        "endUtc": "2999-12-31T23:59:59Z",
        "start": "2026-01-01",
        "end": "2999-12-31",
    },
    "eventDates": {"start": "2026-10-05", "end": "2026-10-09"},
    "location": {"full": "Antwerp, Belgium", "city": "Antwerp", "country": "Belgium"},
    "isOnline": False,
    "cfpLink": "https://sessionize.com/devoxx-be-2026",
}


class _FakeResponse:
    def __init__(self, body: object):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


def _patch_fetch(monkeypatch, by_slug=None, raise_for=None, capture=None):
    """Patch the global urllib.request.urlopen. `by_slug` maps slug -> raw
    event body; `raise_for` maps slug -> exception to raise (per-slug failure);
    `capture` (list) collects (url, X-API-KEY) tuples."""
    by_slug = by_slug or {}
    raise_for = raise_for or {}

    def _fake_urlopen(request, timeout=None):
        url = request.full_url
        slug = url.split("slug=", 1)[1]
        if capture is not None:
            capture.append((url, request.headers.get("X-api-key")))
        if slug in raise_for:
            raise raise_for[slug]
        return _FakeResponse(by_slug.get(slug, _RAW_OPEN_EVENT))

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)


def _entry(entry_id, slug, cohort="stored"):
    return {
        "id": entry_id,
        "cohort": cohort,
        "cfp_url": f"https://sessionize.com/{slug}",
        "source": "sessionize-speaker-api",
    }


def _evidence(run_dir):
    return json.loads((run_dir / "verify-evidence.json").read_text(encoding="utf-8"))


def _drive(module, entries, api_key, run_dir, base=_BASE):
    """drive() takes prepare()'s output; build it here so tests pass entries."""
    return module.drive(module.prepare(entries), base, api_key, run_dir)


# --- normalization port ----------------------------------------------------


def test_normalize_event_maps_nested_fields(verify_sessionize):
    module, _run_dir = verify_sessionize
    r = module.normalize_event(_RAW_OPEN_EVENT, "devoxx-be-2026")
    assert r["name"] == "Devoxx Belgium 2026"
    assert r["cfp_open"] is True
    assert r["cfp_end_local"] == "2999-12-31"
    assert r["conf_start"] == "2026-10-05"
    assert r["city"] == "Antwerp"
    assert r["is_online"] is False
    assert r["cfp_url"] == "https://sessionize.com/devoxx-be-2026"


def test_normalize_event_closed_and_sparse(verify_sessionize):
    module, _run_dir = verify_sessionize
    closed = module.normalize_event({"cfpDates": {"endUtc": "2000-01-01T00:00:00Z"}}, "old")
    assert closed["cfp_open"] is False
    sparse = module.normalize_event({}, "sparse")
    assert sparse["cfp_open"] is False
    assert sparse["city"] is None
    assert sparse["cfp_url"] == "https://sessionize.com/sparse/"


# --- live fetch + apply ----------------------------------------------------


def test_live_response_verifies_and_records_evidence(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    captured: list = []
    _patch_fetch(monkeypatch, by_slug={"conf-a": _RAW_OPEN_EVENT}, capture=captured)

    out = _drive(module, [_entry("s1", "conf-a")], "event-key", run_dir)

    url, api_key_header = captured[0]
    assert url == f"{_BASE}/event?slug=conf-a"
    assert api_key_header == "event-key"
    (decision,) = out["decisions"]
    assert decision["action"] == "verified"
    assert decision["deadline"] == "2999-12-31"
    assert out["evidence"]["live_call"] is True
    assert out["evidence"]["verified"] == 1
    assert _evidence(run_dir)["verified"] == 1


def test_per_slug_failure_isolated_not_cohort_wide(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    # conf-a fails; conf-b succeeds. The failure must not sink conf-b.
    _patch_fetch(
        monkeypatch,
        by_slug={"conf-b": _RAW_OPEN_EVENT},
        raise_for={"conf-a": ConnectionError("boom")},
    )

    out = _drive(module, [_entry("s1", "conf-a"), _entry("s2", "conf-b")], "event-key", run_dir)

    actions = {d["id"]: d["action"] for d in out["decisions"]}
    assert actions == {"s1": "verify_failed", "s2": "verified"}
    assert out["evidence"]["verified"] == 1
    assert out["evidence"]["verify_failed"] == 1


def test_total_outage_marks_all_verify_failed_never_fabricates(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    _patch_fetch(monkeypatch, raise_for={"conf-a": OSError("down"), "conf-b": OSError("down")})

    out = _drive(module, [_entry("s1", "conf-a"), _entry("s2", "conf-b")], "event-key", run_dir)

    assert [d["action"] for d in out["decisions"]] == ["verify_failed", "verify_failed"]
    # Nothing resolved from a live response -> the stamp gate will refuse.
    ev = out["evidence"]
    assert ev["verified"] == 0 and ev["dismissed"] == 0 and ev["dropped"] == 0
    assert ev["verify_failed"] == 2


def test_non_object_response_is_verify_failed(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    _patch_fetch(monkeypatch, by_slug={"conf-a": ["not", "an", "object"]})

    out = _drive(module, [_entry("s1", "conf-a")], "event-key", run_dir)

    assert out["decisions"][0]["action"] == "verify_failed"


def test_empty_slug_set_makes_no_call(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize

    def _boom(request, timeout=None):
        raise AssertionError("no events call should happen with an empty slug set")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    entry = {
        "id": "d1",
        "cohort": "stored",
        "cfp_url": "https://developers.events/x",
        "source": "developers.events",
    }
    out = _drive(module, [entry], "", run_dir)

    assert out["evidence"]["slugs_expected"] == 0
    assert out["evidence"]["sessionize_total"] == 0
    assert out["evidence"]["live_call"] is False
    assert out["non_sessionize"] == ["d1"]


def test_unverifiable_sessionize_entry_counts_in_cohort(verify_sessionize, monkeypatch):
    # A Sessionize-sourced entry whose cfp_url has no scheme/netloc yields no
    # derivable slug -> prep.unverifiable -> verify_failed, with slugs_expected
    # == 0. sessionize_total must still count it so the stamp gate does NOT read
    # the run as "nothing to verify" (jbaruch/nanoclaw-conferences#8, stateful-artifacts).
    module, run_dir = verify_sessionize

    def _boom(request, timeout=None):
        raise AssertionError("no events call: an unverifiable entry has no slug to fetch")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    entry = {
        "id": "u1",
        "cohort": "stored",
        "source": "sessionize-speaker-api",
        "cfp_url": "not-a-url",
    }
    out = _drive(module, [entry], "event-key", run_dir)

    assert out["decisions"][0]["action"] == "verify_failed"
    ev = out["evidence"]
    assert ev["slugs_expected"] == 0
    assert ev["sessionize_total"] == 1
    assert ev["verify_failed"] == 1
    assert _evidence(run_dir)["sessionize_total"] == 1


# --- main() I/O + config ---------------------------------------------------


def test_main_missing_key_with_slugs_exits_1(verify_sessionize, monkeypatch, capsys):
    module, _run_dir = verify_sessionize
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_entry("s1", "conf-a")])))

    code = module.main([])

    assert code == 1
    assert "SESSIONIZE_EVENT_API_KEY" in capsys.readouterr().err


def test_main_malformed_stdin_exits_1(verify_sessionize, monkeypatch, capsys):
    module, _run_dir = verify_sessionize
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))

    code = module.main([])

    assert code == 1
    assert "not valid JSON" in capsys.readouterr().err


@pytest.mark.parametrize(
    "stdin,needle",
    [
        ('"a string"', "expected a JSON array"),
        ("[1]", "every entry must be an object"),
        ('[{"cohort": "stored"}]', "non-empty string `id`"),
        ('[{"id": "x", "cohort": "bogus"}]', '`cohort` of "new" or "stored"'),
    ],
)
def test_main_rejects_malformed_entries(verify_sessionize, monkeypatch, capsys, stdin, needle):
    module, _run_dir = verify_sessionize
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))

    code = module.main([])

    assert code == 1
    assert needle in capsys.readouterr().err


def test_main_happy_path_writes_evidence_and_stdout(verify_sessionize, monkeypatch, capsys):
    module, run_dir = verify_sessionize
    monkeypatch.setenv("SESSIONIZE_EVENT_API_KEY", "event-key")
    _patch_fetch(monkeypatch, by_slug={"conf-a": _RAW_OPEN_EVENT})
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_entry("s1", "conf-a")])))

    code = module.main([])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["evidence"]["verified"] == 1
    assert _evidence(run_dir)["verified"] == 1
