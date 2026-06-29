"""Tests for skills/check-cfps/scripts/verify-sessionize.py.

The driver collapses prepare -> live events call -> apply into one step so the
Sessionize round-trip can't be skipped by the agent (jbaruch/nanoclaw-conferences#7),
and writes the verification-evidence marker the stamp gate reads
(jbaruch/nanoclaw-conferences#8).

Contract under test:
  - happy path: a live response drives apply; evidence records live_call=true.
  - cohort-wide API failure: NO fabricated verdicts — every Sessionize entry
    resolves to verify_failed, evidence.live_call=false, exit 0 (the
    verify_failed decisions are the product Step 8 persists).
  - empty Sessionize slug set: no call, evidence.live_call=false,
    slugs_expected=0 (the stamp gate treats this as nothing-to-verify, clean).
  - missing SESSIONIZE_API_BASE with slugs to verify -> exit 1, no evidence.
  - malformed stdin -> exit 1.
"""

from __future__ import annotations

import io
import json

import pytest


class _FakeResponse:
    """Minimal `urllib.request.urlopen` return: context manager with `.read()`
    yielding UTF-8 bytes, matching the real `.read()` the driver decodes."""

    def __init__(self, body: object):
        self._body = json.dumps(body).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


def _patch_events(monkeypatch, *, response=None, raise_exc=None, capture=None):
    """Patch the global `urllib.request.urlopen` the driver calls. `response`
    is the JSON body to return; `raise_exc` is raised instead (transport
    failure). `capture` (a dict) records the POSTed slugs for assertions."""

    def _fake_urlopen(request, timeout=None):
        if capture is not None:
            capture["body"] = json.loads(request.data.decode("utf-8"))
        if raise_exc is not None:
            raise raise_exc
        return _FakeResponse(response)

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


def _drive(module, entries, base, token, run_dir):
    """drive() takes prepare()'s output; build it here so tests pass entries."""
    return module.drive(module.prepare(entries), base, token, run_dir)


def test_live_response_verifies_and_records_evidence(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    monkeypatch.setenv("SESSIONIZE_API_BASE", "https://host.local/sessionize")
    captured: dict = {}
    _patch_events(
        monkeypatch,
        response=[
            {"slug": "conf-a", "cfp_open": True, "is_online": False, "cfp_end_local": "2026-09-01"}
        ],
        capture=captured,
    )

    out = _drive(module, [_entry("s1", "conf-a")], "https://host.local/sessionize", None, run_dir)

    assert captured["body"] == {"slugs": ["conf-a"]}
    (decision,) = out["decisions"]
    assert decision["action"] == "verified"
    assert decision["deadline"] == "2026-09-01"
    assert out["evidence"]["live_call"] is True
    assert out["evidence"]["slugs_expected"] == 1
    assert out["evidence"]["verified"] == 1
    assert _evidence(run_dir)["live_call"] is True


def test_api_failure_marks_verify_failed_never_fabricates(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    monkeypatch.setenv("SESSIONIZE_API_BASE", "https://host.local/sessionize")
    _patch_events(monkeypatch, raise_exc=ConnectionError("boom"))

    out = _drive(
        module,
        [_entry("s1", "conf-a"), _entry("s2", "conf-b")],
        "https://host.local/sessionize",
        None,
        run_dir,
    )

    # No remembered verdicts: every Sessionize entry is verify_failed.
    assert [d["action"] for d in out["decisions"]] == ["verify_failed", "verify_failed"]
    assert out["results"] == []
    assert out["evidence"]["live_call"] is False
    assert out["evidence"]["verify_failed"] == 2
    assert _evidence(run_dir)["live_call"] is False


def test_non_array_response_is_cohort_failure(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    monkeypatch.setenv("SESSIONIZE_API_BASE", "https://host.local/sessionize")
    _patch_events(monkeypatch, response={"oops": "not a list"})

    out = _drive(module, [_entry("s1", "conf-a")], "https://host.local/sessionize", None, run_dir)

    assert out["decisions"][0]["action"] == "verify_failed"
    assert out["evidence"]["live_call"] is False


def test_empty_slug_set_makes_no_call(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize

    def _boom(request, timeout=None):
        raise AssertionError("no events call should happen with an empty slug set")

    monkeypatch.setattr("urllib.request.urlopen", _boom)

    # A developers.events entry: non-Sessionize, no slug to verify.
    entry = {
        "id": "d1",
        "cohort": "stored",
        "cfp_url": "https://developers.events/x",
        "source": "developers.events",
    }
    out = _drive(module, [entry], None, None, run_dir)

    assert out["evidence"]["slugs_expected"] == 0
    assert out["evidence"]["live_call"] is False
    assert out["non_sessionize"] == ["d1"]
    assert _evidence(run_dir)["slugs_expected"] == 0


def test_token_sent_as_bearer(verify_sessionize, monkeypatch):
    module, run_dir = verify_sessionize
    sent: dict = {}

    def _fake_urlopen(request, timeout=None):
        sent["auth"] = request.headers.get("Authorization")
        return _FakeResponse(
            [
                {
                    "slug": "conf-a",
                    "cfp_open": True,
                    "is_online": False,
                    "cfp_end_local": "2026-09-01",
                }
            ]
        )

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    _drive(
        module, [_entry("s1", "conf-a")], "https://host.local/sessionize", "secret-token", run_dir
    )

    assert sent["auth"] == "Bearer secret-token"


def test_main_missing_base_with_slugs_exits_1(verify_sessionize, monkeypatch, capsys):
    module, _run_dir = verify_sessionize
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_entry("s1", "conf-a")])))

    code = module.main([])

    assert code == 1
    assert "SESSIONIZE_API_BASE" in capsys.readouterr().err


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
    monkeypatch.setenv("SESSIONIZE_API_BASE", "https://host.local/sessionize")
    _patch_events(
        monkeypatch,
        response=[
            {"slug": "conf-a", "cfp_open": True, "is_online": False, "cfp_end_local": "2026-09-01"}
        ],
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps([_entry("s1", "conf-a")])))

    code = module.main([])
    out = json.loads(capsys.readouterr().out)

    assert code == 0
    assert out["evidence"]["live_call"] is True
    assert _evidence(run_dir)["live_call"] is True
