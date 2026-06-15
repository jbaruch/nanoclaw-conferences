"""Tests for skills/check-cfps/scripts/apply-sessionize-results.py.

Contract (Step 5 result application):
  - cfp_open False -> drop (new) / dismiss-MISSED (stored).
  - is_online True -> drop (new) / dismiss-online (stored).
  - otherwise -> verified, deadline = cfp_end_local[:10].
  - missing result or {slug, error} -> verify_failed; prep `unverifiable`
    ids -> verify_failed.
  - one result fans out to every entry sharing the slug.
  - pure stdin->stdout JSON; never writes state.
"""

from __future__ import annotations

import io
import json


def _run(module, monkeypatch, capsys, stdin: str):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    code = module.main([])
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _prep(sessionize, unverifiable=None):
    return {"sessionize": sessionize, "unverifiable": unverifiable or []}


def test_closed_cfp_dismisses_stored_entry_with_missed_note(
    apply_sessionize_results,
):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s1", "cohort": "stored", "slug": "old-conf"}]),
        [{"slug": "old-conf", "cfp_open": False, "cfp_end_local": "2026-01-09"}],
    )
    (decision,) = out["decisions"]
    assert decision["action"] == "dismiss"
    assert decision["bot_notes"] == (
        "Dismissed: MISSED — CFP closed on 2026-01-09. Verified via Sessionize."
    )
    assert out["summary"]["dismissed"] == 1


def test_closed_stored_without_end_date_is_verify_failed_not_none_dismissal(
    apply_sessionize_results,
):
    # A closed result with no cfp_end_local must not persist a
    # "CFP closed on None" dismissal — it's malformed, so verify_failed.
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s7", "cohort": "stored", "slug": "dateless"}]),
        [{"slug": "dateless", "cfp_open": False}],
    )
    assert out["decisions"][0]["action"] == "verify_failed"


def test_closed_cfp_drops_new_candidate(apply_sessionize_results):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "n1", "cohort": "new", "slug": "old-conf"}]),
        [{"slug": "old-conf", "cfp_open": False, "cfp_end_local": "2026-01-09"}],
    )
    assert out["decisions"][0]["action"] == "drop"
    assert out["summary"]["dropped"] == 1


def test_online_event_dismisses_stored_entry(apply_sessionize_results):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s2", "cohort": "stored", "slug": "virtual-conf"}]),
        [{"slug": "virtual-conf", "cfp_open": True, "is_online": True}],
    )
    decision = out["decisions"][0]
    assert decision["action"] == "dismiss"
    assert decision["bot_notes"] == "Dismissed: online/virtual event."


def test_online_event_drops_new_candidate(apply_sessionize_results):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "n2", "cohort": "new", "slug": "virtual-conf"}]),
        [{"slug": "virtual-conf", "cfp_open": True, "is_online": True}],
    )
    assert out["decisions"][0]["action"] == "drop"


def test_open_in_person_cfp_is_verified_with_refreshed_deadline(
    apply_sessionize_results,
):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s3", "cohort": "stored", "slug": "devoxx-be-2026"}]),
        [
            {
                "slug": "devoxx-be-2026",
                "cfp_open": True,
                "is_online": False,
                "cfp_end_local": "2026-08-15T23:59:59",
            }
        ],
    )
    decision = out["decisions"][0]
    assert decision["action"] == "verified"
    assert decision["deadline"] == "2026-08-15"
    assert decision["cfp_end_local"] == "2026-08-15T23:59:59"
    # The full event rides along so the caller attaches its fields without
    # re-joining results to entries.
    assert decision["event"]["slug"] == "devoxx-be-2026"
    assert out["summary"]["verified"] == 1


def test_malformed_success_without_cfp_end_local_is_verify_failed(
    apply_sessionize_results,
):
    # Open + in-person but no usable CFP end date: don't stamp a verified
    # (null-deadline) decision that would let Step 8 advance last_verified
    # on an unreadable response.
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s6", "cohort": "stored", "slug": "weird"}]),
        [{"slug": "weird", "cfp_open": True, "is_online": False}],
    )
    assert out["decisions"][0]["action"] == "verify_failed"


def test_missing_result_is_verify_failed(apply_sessionize_results):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s4", "cohort": "stored", "slug": "absent-slug"}]),
        [],
    )
    assert out["decisions"][0]["action"] == "verify_failed"


def test_error_result_is_verify_failed(apply_sessionize_results):
    out = apply_sessionize_results.apply_results(
        _prep([{"id": "s5", "cohort": "stored", "slug": "boom"}]),
        [{"slug": "boom", "error": "Sessionize API returned 404: Not Found"}],
    )
    assert out["decisions"][0]["action"] == "verify_failed"
    assert out["summary"]["verify_failed"] == 1


def test_unverifiable_entries_become_verify_failed_keeping_cohort(
    apply_sessionize_results,
):
    out = apply_sessionize_results.apply_results(
        _prep(
            [],
            unverifiable=[
                {"id": "no-url-1", "cohort": "stored"},
                {"id": "no-url-2", "cohort": "new"},
            ],
        ),
        [],
    )
    by_id = {d["id"]: d for d in out["decisions"]}
    assert by_id["no-url-1"] == {
        "id": "no-url-1",
        "cohort": "stored",
        "action": "verify_failed",
    }
    assert by_id["no-url-2"]["cohort"] == "new"


def test_one_result_fans_out_to_every_entry_sharing_the_slug(
    apply_sessionize_results,
):
    # A stored entry and a new candidate resolving to the same slug must
    # BOTH pick up the single result — no duplicate left unverified.
    out = apply_sessionize_results.apply_results(
        _prep(
            [
                {"id": "stored-key", "cohort": "stored", "slug": "devoxx-be-2026"},
                {"id": "devoxx-be-2026", "cohort": "new", "slug": "devoxx-be-2026"},
            ]
        ),
        [
            {
                "slug": "devoxx-be-2026",
                "cfp_open": True,
                "is_online": False,
                "cfp_end_local": "2026-08-15",
            }
        ],
    )
    assert [d["action"] for d in out["decisions"]] == ["verified", "verified"]
    assert out["summary"]["verified"] == 2


def test_main_emits_json_and_exits_zero(apply_sessionize_results, monkeypatch, capsys):
    payload = {
        "prep": _prep([{"id": "s1", "cohort": "stored", "slug": "k"}]),
        "results": [
            {"slug": "k", "cfp_open": True, "is_online": False, "cfp_end_local": "2026-09-01"}
        ],
    }
    code, out, _ = _run(apply_sessionize_results, monkeypatch, capsys, json.dumps(payload))
    assert code == 0
    assert json.loads(out)["decisions"][0]["action"] == "verified"


def test_main_rejects_invalid_json(apply_sessionize_results, monkeypatch, capsys):
    code, _, err = _run(apply_sessionize_results, monkeypatch, capsys, "nope")
    assert code == 1
    assert "not valid JSON" in err


def test_main_rejects_missing_prep(apply_sessionize_results, monkeypatch, capsys):
    code, _, err = _run(
        apply_sessionize_results,
        monkeypatch,
        capsys,
        json.dumps({"results": []}),
    )
    assert code == 1
    assert "prep" in err


def test_main_rejects_malformed_prep_section(apply_sessionize_results, monkeypatch, capsys):
    # prep.sessionize holding non-objects would crash apply_results — reject
    # it deterministically with a diagnostic instead.
    code, _, err = _run(
        apply_sessionize_results,
        monkeypatch,
        capsys,
        json.dumps({"prep": {"sessionize": ["not-an-object"]}, "results": []}),
    )
    assert code == 1
    assert "prep.sessionize" in err
