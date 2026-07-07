"""Unit tests for skills/check-cfps/scripts/audit-sessionize-key-drift.py.

Locks down the documented contract:
  - Walks only sessionize-sourced entries; skips other sources, _-prefixed
    config keys, and non-dict values without crashing.
  - For each, derives slug from cfp_url's first path segment and reports
    drift when the dict key disagrees with the derived slug.
  - Missing or unparseable cfp_url goes to a separate `missing_cfp_url`
    bucket so the operator can fix the entry instead of silently rolling
    it into the drift list.
  - Report-only: never mutates the state file (verified by mtime
    invariance + content snapshot).
  - Missing state file is no-op exit 0; corrupt/non-dict state is exit 1
    with stderr diagnostic.
"""

import json

import pytest


def _payload(captured):
    return json.loads(captured.out.strip().splitlines()[-1])


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_missing_state_file_is_noop_exit_0(audit_sessionize_key_drift, tmp_path, capsys):
    state_path = tmp_path / "absent.json"
    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    assert _payload(captured) == {"checked": 0, "drifted": [], "missing_cfp_url": []}
    assert "state file not found" in captured.err


def test_corrupt_json_exits_1(audit_sessionize_key_drift, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not valid", encoding="utf-8")

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_invalid_utf8_exits_1(audit_sessionize_key_drift, tmp_path, capsys):
    """A state file that is not valid UTF-8 gets the same exit-1 stderr
    diagnostic as malformed JSON, not an unhandled UnicodeDecodeError."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_bytes(b"\xff\xfe{}")

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err


def test_root_not_dict_exits_1(audit_sessionize_key_drift, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps([]), encoding="utf-8")

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected dict" in captured.err


def test_clean_state_no_drift(audit_sessionize_key_drift, tmp_path, capsys):
    """Sessionize entry whose key matches the URL slug — no drift reported,
    `checked` counts the entry."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "devoxx-fr-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload == {"checked": 1, "drifted": [], "missing_cfp_url": []}


def test_drift_reported_with_resolved_slug(audit_sessionize_key_drift, tmp_path, capsys):
    """Sessionize entry whose key carries the canonical drift pattern from
    nanoclaw-admin#192 (versioning suffix added) — the drift entry names
    the dict key, the URL-derived slug, and the cfp_url so the operator
    can decide whether to renormalize."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "agentcon-miami-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/agentcon-miami",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["checked"] == 1
    assert payload["missing_cfp_url"] == []
    assert payload["drifted"] == [
        {
            "key": "agentcon-miami-2026",
            "url_slug": "agentcon-miami",
            "cfp_url": "https://sessionize.com/agentcon-miami",
        }
    ]


def test_missing_cfp_url_reported_separately(audit_sessionize_key_drift, tmp_path, capsys):
    """A sessionize-sourced entry with no cfp_url can't have its slug
    derived. Report it under `missing_cfp_url`, not `drifted` — that's
    the operator's signal to fix the entry rather than guess the slug."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "orphan-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["checked"] == 1
    assert payload["drifted"] == []
    assert payload["missing_cfp_url"] == ["orphan-2026"]


def test_unparseable_cfp_url_goes_to_missing(audit_sessionize_key_drift, tmp_path, capsys):
    """A cfp_url that is a bare string with no path (e.g.
    `https://sessionize.com`) yields no first-path-segment slug —
    treated the same as missing rather than a synthetic empty drift."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "weird-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["missing_cfp_url"] == ["weird-2026"]
    assert payload["drifted"] == []


def test_non_sessionize_entries_skipped(audit_sessionize_key_drift, tmp_path, capsys):
    """Entries from `developers.events`, `javaconferences.org`, and
    unsourced legacy don't enter the audit — they don't go through the
    Sessionize live API call so key/url-slug drift can't 404 them."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
                "volcamp-2026": {
                    "source": "javaconferences.org",
                    "cfp_url": "https://javaconferences.org/conferences/volcamp-2026",
                },
                "legacy-2026": {
                    "cfp_url": "https://example.org/some-legacy-cfp",
                },
                "devoxx-fr-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["checked"] == 1
    assert payload["drifted"] == []
    assert payload["missing_cfp_url"] == []


def test_underscore_keys_and_non_dict_entries_skipped(audit_sessionize_key_drift, tmp_path, capsys):
    """`_blocked_prefixes` and historical list-shaped entries are not CFP
    records — skip without crashing or counting."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["devopsdays"],
                "broken-2026": ["not", "a", "dict"],
                "real-sessionize-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/real-sessionize-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["checked"] == 1


def test_audit_is_report_only(audit_sessionize_key_drift, tmp_path, capsys):
    """Running the audit must NOT mutate the state file — neither
    content nor mtime changes. The contract is report-only because
    rewriting the dict key would change the entry's primary identity
    in a way downstream code may reference (issue's proposed shape #2
    explicitly defers the rewrite decision)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "agentcon-miami-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/agentcon-miami",
                },
            }
        ),
        encoding="utf-8",
    )
    pre_content = state_path.read_text(encoding="utf-8")
    pre_mtime = state_path.stat().st_mtime_ns

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert state_path.read_text(encoding="utf-8") == pre_content
    assert state_path.stat().st_mtime_ns == pre_mtime


def test_realistic_cohort_aggregates(audit_sessionize_key_drift, tmp_path, capsys):
    """Cohort exercising the four drift patterns enumerated in
    nanoclaw-admin#192's evidence section: versioning suffix, aliasing,
    regional disambiguation, abbreviation->fullname. Plus a clean
    Sessionize entry and an out-of-scope entry."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                # Versioning suffix
                "agentcon-miami-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/agentcon-miami",
                },
                # Aliasing
                "porto-tech-hub-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/pth-2026",
                },
                # Regional disambiguation
                "infobip-shift-zadar-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/infobip-shift-2026",
                },
                # Abbreviation -> fullname
                "workplace-security-ninja-user-group-switzerland-2603-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/wpninja-ug-ch",
                },
                # Clean (no drift)
                "devoxx-fr-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
                # Out of scope (different source — not audited)
                "lambda-world-2026": {
                    "source": "developers.events",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = audit_sessionize_key_drift.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _payload(captured)
    assert payload["checked"] == 5  # 5 sessionize entries; 1 non-sessionize skipped
    assert payload["missing_cfp_url"] == []
    drifted_keys = {d["key"] for d in payload["drifted"]}
    assert drifted_keys == {
        "agentcon-miami-2026",
        "porto-tech-hub-2026",
        "infobip-shift-zadar-2026",
        "workplace-security-ninja-user-group-switzerland-2603-2026",
    }
    # Each drift line carries the resolved slug too.
    pth = next(d for d in payload["drifted"] if d["key"] == "porto-tech-hub-2026")
    assert pth["url_slug"] == "pth-2026"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://sessionize.com/devoxx-fr-2026", "devoxx-fr-2026"),
        ("https://sessionize.com/devoxx-fr-2026/", "devoxx-fr-2026"),
        ("http://sessionize.com/agentcon-miami", "agentcon-miami"),
        ("https://events.sessionize.com/some-event-2026", "some-event-2026"),
        # Trailing path segments after the slug — Sessionize puts the
        # event slug first; later segments are subpages we don't want.
        ("https://sessionize.com/devoxx-fr-2026/sessions", "devoxx-fr-2026"),
        # Bare host or empty path → None
        ("https://sessionize.com", None),
        ("https://sessionize.com/", None),
        ("", None),
        # Non-string defends against schema drift in cfp_url
        (None, None),
        # Scheme-less inputs: urlparse puts the whole thing in `.path`
        # with empty `.scheme`/`.netloc`. Without the scheme+netloc
        # guard, `derive_slug("sessionize.com/foo")` would return
        # `"sessionize.com"` and produce a fake drift entry. Require a
        # real URL — these cases land in `missing_cfp_url` instead.
        ("sessionize.com/devoxx-fr-2026", None),
        ("sessionize.com", None),
        ("/just-a-path-2026", None),
    ],
)
def test_derive_slug_unit(audit_sessionize_key_drift, url, expected):
    """Direct unit test of derive_slug — locks down the parsing edges
    (trailing slash, deeper subpaths, subdomains, missing path,
    non-string input) without going through main()."""
    assert audit_sessionize_key_drift.derive_slug(url) == expected
