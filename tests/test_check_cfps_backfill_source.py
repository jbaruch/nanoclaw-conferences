"""Unit tests for skills/check-cfps/scripts/backfill-source.py.

Locks down the script's documented contract:
  - Idempotent: running on already-sourced entries leaves them alone.
  - Source inferred from cfp_url host (sessionize.com / developers.events
    / javaconferences.org), including subdomains.
  - Unknown / missing host leaves entry unsourced (Step 4 will treat
    unsourced as non-Sessionize, the safe default).
  - Underscore-prefixed keys (e.g. _blocked_prefixes) are skipped.
  - Non-dict entry shapes are skipped without crashing.
  - Missing state file is a no-op exit 0.
  - Read/write errors exit 1 with stderr diagnostic.
  - JSON output (last line of stdout) carries counters per the documented shape.
"""

import json
from pathlib import Path

import pytest


def _read_state(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stdout_payload(captured):
    """Return the parsed JSON payload from the script's stdout."""
    return json.loads(captured.out.strip().splitlines()[-1])


def test_missing_state_file_is_no_op_exit_0(backfill_source, tmp_path, capsys):
    """No state file at the configured path is a documented no-op:
    exit 0 with zero counters and a stderr diagnostic. The skill ships
    with the script + the in-Step-4 lazy inference, so the explicit
    backfill being a no-op on a fresh deploy is the intended path."""
    state_path = tmp_path / "absent.json"
    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload == {
        "backfilled": 0,
        "skipped_existing_source": 0,
        "unsourced_remaining": 0,
        "by_source": {},
    }
    assert "state file not found" in captured.err


def test_corrupt_json_exits_1_with_stderr(backfill_source, tmp_path, capsys):
    """Malformed JSON on disk is a hard error — exit 1 + stderr
    diagnostic naming the path. Silently writing zeros would mask
    the operator's actual problem (the script can't tell whether a
    parse error means 'empty state' or 'truncated write')."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text("{not valid json", encoding="utf-8")

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "failed to read" in captured.err
    assert str(state_path) in captured.err


def test_root_not_dict_exits_1(backfill_source, tmp_path, capsys):
    """If the JSON parses but the root is a list/scalar instead of a
    dict, the file isn't a cfp-state.json — refuse to write rather
    than guess a structure."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 1
    assert "expected dict" in captured.err


def test_existing_source_left_alone(backfill_source, tmp_path, capsys):
    """An entry that already has `source` set is skipped — backfill is
    additive, never overwrites a prior decision."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "devoxx-fr-2026": {
                    "status": "open",
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://developers.events/cfps/devoxx-fr-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["skipped_existing_source"] == 1
    assert payload["backfilled"] == 0
    # Pre-existing source is preserved exactly — not overwritten by host inference.
    assert _read_state(state_path)["devoxx-fr-2026"]["source"] == "sessionize-speaker-api"


def test_infer_sessionize_from_host(backfill_source, tmp_path, capsys):
    """sessionize.com (root) → sessionize-speaker-api."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "devoxx-fr-2026": {
                    "status": "open",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 1
    assert payload["by_source"] == {"sessionize-speaker-api": 1}
    assert _read_state(state_path)["devoxx-fr-2026"]["source"] == "sessionize-speaker-api"


def test_infer_sessionize_from_subdomain(backfill_source, tmp_path, capsys):
    """events.sessionize.com (subdomain) → sessionize-speaker-api.
    Sessionize routes some events through subdomains; the host check
    must allow `*.sessionize.com` to land on the canonical source."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "subevent-2026": {
                    "status": "open",
                    "cfp_url": "https://events.sessionize.com/subevent-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert _read_state(state_path)["subevent-2026"]["source"] == "sessionize-speaker-api"


def test_infer_developers_events(backfill_source, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert _read_state(state_path)["lambda-world-2026"]["source"] == "developers.events"


def test_infer_javaconferences(backfill_source, tmp_path, capsys):
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "volcamp-2026": {
                    "status": "open",
                    "cfp_url": "https://javaconferences.org/conferences/volcamp-2026",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert _read_state(state_path)["volcamp-2026"]["source"] == "javaconferences.org"


def test_unknown_host_left_unsourced(backfill_source, tmp_path, capsys):
    """A host that doesn't match any known feed → entry stays
    unsourced. Step 4's non-Sessionize branch is the safe default
    here; nothing in this script should guess a source from a host
    it doesn't recognize."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "ad-hoc-conf-2026": {
                    "status": "open",
                    "cfp_url": "https://random-conf.example.com/cfp",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 0
    assert payload["unsourced_remaining"] == 1
    assert "source" not in _read_state(state_path)["ad-hoc-conf-2026"]


def test_missing_cfp_url_left_unsourced(backfill_source, tmp_path, capsys):
    """An entry with no cfp_url field at all (the truly-legacy shape)
    cannot have its source inferred — leave it alone, count it in
    unsourced_remaining for operator visibility."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps({"orphan-2026": {"status": "open"}}),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["unsourced_remaining"] == 1
    assert "source" not in _read_state(state_path)["orphan-2026"]


def test_underscore_prefixed_keys_skipped(backfill_source, tmp_path, capsys):
    """`_blocked_prefixes` and other config keys are not CFP records
    — they're never counted, never mutated. The script must walk
    around them the same way the runtime skill does."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": ["devopsdays"],
                "_some_other_config": {"value": 1},
                "real-cfp-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/real-cfp-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    # Only the real CFP is counted — the two _-prefixed keys are skipped entirely.
    assert payload["backfilled"] == 1
    assert payload["unsourced_remaining"] == 0
    written = _read_state(state_path)
    # Underscore keys preserved unchanged.
    assert written["_blocked_prefixes"] == ["devopsdays"]
    assert written["_some_other_config"] == {"value": 1}


def test_non_dict_entry_skipped(backfill_source, tmp_path, capsys):
    """A list or string where a CFP record should be (the historical
    schema-drift shape from morning-brief-cfp's #46 bug) is skipped
    silently rather than crashing the backfill."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "broken-2026": ["not", "a", "dict"],
                "real-cfp-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/real-cfp-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    # Broken entry is invisible to the script; only the real one is processed.
    assert payload["backfilled"] == 1
    assert payload["unsourced_remaining"] == 0
    # The broken entry is still on disk, untouched.
    assert _read_state(state_path)["broken-2026"] == ["not", "a", "dict"]


def test_idempotent_second_run_is_noop(backfill_source, tmp_path, capsys):
    """Running the script twice must not re-mutate already-sourced
    entries. Second run reports `backfilled: 0` and the state file's
    mtime would not change in a real filesystem (the script only
    writes when backfilled > 0)."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
            }
        ),
        encoding="utf-8",
    )

    # First run: backfills the entry.
    rc1 = backfill_source.main(["--state-path", str(state_path)])
    captured1 = capsys.readouterr()
    payload1 = _stdout_payload(captured1)
    mtime_after_first = state_path.stat().st_mtime_ns
    assert rc1 == 0
    assert payload1["backfilled"] == 1

    # Second run: nothing to do.
    rc2 = backfill_source.main(["--state-path", str(state_path)])
    captured2 = capsys.readouterr()
    payload2 = _stdout_payload(captured2)

    assert rc2 == 0
    assert payload2["backfilled"] == 0
    assert payload2["skipped_existing_source"] == 1
    # No write happened on the second run (mtime unchanged).
    assert state_path.stat().st_mtime_ns == mtime_after_first


def test_mixed_cohort_counts_aggregated(backfill_source, tmp_path, capsys):
    """A realistic state file has all four cohorts at once; the script
    must aggregate per-source counts correctly across them."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "_blocked_prefixes": [],
                "devoxx-fr-2026": {
                    "source": "sessionize-speaker-api",
                    "cfp_url": "https://sessionize.com/devoxx-fr-2026",
                },
                "lambda-world-2026": {
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
                "volcamp-2026": {
                    "cfp_url": "https://javaconferences.org/conferences/volcamp-2026",
                },
                "ad-hoc-2026": {
                    "cfp_url": "https://random-conf.example.com/cfp",
                },
                "no-url-2026": {"status": "open"},
            }
        ),
        encoding="utf-8",
    )

    rc = backfill_source.main(["--state-path", str(state_path)])
    captured = capsys.readouterr()

    assert rc == 0
    payload = _stdout_payload(captured)
    assert payload["backfilled"] == 2  # lambda-world + volcamp
    assert payload["skipped_existing_source"] == 1  # devoxx-fr
    assert payload["unsourced_remaining"] == 2  # ad-hoc + no-url
    assert payload["by_source"] == {
        "sessionize-speaker-api": 1,
        "developers.events": 1,
        "javaconferences.org": 1,
    }


def test_atomic_write_via_replace(backfill_source, tmp_path, capsys, monkeypatch):
    """The script writes through a sibling temp file + os.replace, not
    a plain write_text on the target. Concurrent containers run on
    the same /workspace/group/ directory, so a plain write would
    race with a parallel `check-cfps` or `morning-brief --mark-shown`
    write and silently drop one side. Verify by intercepting
    os.replace and confirming (a) the call hits the target path,
    (b) the source path is a sibling temp file in the same directory
    (so the replace is intra-filesystem and atomic), and (c) the
    sibling has the new content while the target still holds the
    pre-write content at intercept time."""
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(
        json.dumps(
            {
                "lambda-world-2026": {
                    "status": "open",
                    "cfp_url": "https://developers.events/cfps/lambda-world-2026/",
                },
            }
        ),
        encoding="utf-8",
    )
    pre_write_content = state_path.read_text(encoding="utf-8")

    captured_calls = []

    real_replace = backfill_source.os.replace

    def spying_replace(src, dst):
        # Capture the moment os.replace is invoked — at this point the
        # tmp file should hold the new content while the target still
        # holds the original.
        src_path = Path(src)
        dst_path = Path(dst)
        captured_calls.append(
            {
                "src": src_path,
                "dst": dst_path,
                "src_content": src_path.read_text(encoding="utf-8"),
                "dst_content_at_replace": dst_path.read_text(encoding="utf-8"),
            }
        )
        return real_replace(src, dst)

    monkeypatch.setattr(backfill_source.os, "replace", spying_replace)

    rc = backfill_source.main(["--state-path", str(state_path)])
    _ = capsys.readouterr()

    assert rc == 0
    assert len(captured_calls) == 1, "expected exactly one os.replace call on the write path"
    call = captured_calls[0]

    # (a) replace targets the configured state path.
    assert call["dst"] == state_path

    # (b) source is a sibling in the same directory (intra-filesystem
    # — required for replace to be atomic).
    assert call["src"].parent == state_path.parent
    assert call["src"] != state_path

    # (c) at the moment replace fires, the sibling already holds the
    # new content (with the inferred source) while the target still
    # holds the pre-write content. This is the atomicity guarantee:
    # a concurrent reader sees either the old file or the new file,
    # never a partial one.
    assert '"source": "developers.events"' in call["src_content"]
    assert call["dst_content_at_replace"] == pre_write_content

    # And after the call returns, the target holds the new content
    # and no orphan tmp file is left behind.
    final = json.loads(state_path.read_text(encoding="utf-8"))
    assert final["lambda-world-2026"]["source"] == "developers.events"
    siblings = [p for p in tmp_path.iterdir() if p.name != "cfp-state.json"]
    assert siblings == [], f"orphan temp files left behind: {siblings}"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://sessionize.com/foo", "sessionize-speaker-api"),
        ("http://sessionize.com/foo", "sessionize-speaker-api"),
        ("https://EVENTS.SESSIONIZE.COM/foo", "sessionize-speaker-api"),
        ("https://developers.events/foo", "developers.events"),
        ("https://www.developers.events/foo", "developers.events"),
        ("https://javaconferences.org/foo", "javaconferences.org"),
        ("https://other.example.com/foo", None),
        ("", None),
        ("not-a-url", None),
        # `notsessionize.com` must NOT be treated as sessionize — the
        # subdomain check requires a leading dot.
        ("https://notsessionize.com/foo", None),
    ],
)
def test_infer_source_unit(backfill_source, url, expected):
    """Direct unit test on the infer_source helper — covers host
    matching edge cases (case insensitivity, subdomains, empty input,
    suffix-collision avoidance) without going through the full
    main() roundtrip."""
    assert backfill_source.infer_source(url) == expected
