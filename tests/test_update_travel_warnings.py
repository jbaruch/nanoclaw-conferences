"""Outcome tests for travel-warning updates before the locked state commit."""

import copy
import io
import json
import subprocess
import sys

import pytest

WARNING = "Could not verify travel conflict — exact conference dates unknown."


def test_unknown_dates_are_idempotent_and_preserve_sticky_notes(update_travel_warnings):
    record = {
        "status": "approved",
        "shown_in_brief": True,
        "bot_notes": "Auto-approved: javaconferences.org source",
        "conf_date": "",
        "matched_interests": ["java"],
    }
    original = copy.deepcopy(record)
    payload = {"entries": {"conf": record}, "exact_dates_known": {"conf": False}}
    first = update_travel_warnings.update(payload)
    second = update_travel_warnings.update({**payload, "entries": first})
    assert first == second
    assert first["conf"] == {**record, "bot_notes": record["bot_notes"] + " " + WARNING}
    assert record == original


def test_known_dates_remove_warning_but_keep_other_notes(update_travel_warnings):
    notes = "⚠️ STALE DATA — API failed. Relevant to Java."
    result = update_travel_warnings.update(
        {
            "entries": {"conf": {"status": "conflict", "bot_notes": notes + " " + WARNING}},
            "exact_dates_known": {"conf": True},
        }
    )
    assert result["conf"] == {"status": "conflict", "bot_notes": notes}


@pytest.mark.parametrize("known", [False, True])
def test_duplicate_legacy_warnings_are_removed(update_travel_warnings, known):
    result = update_travel_warnings.update(
        {
            "entries": {"conf": {"status": "open", "bot_notes": WARNING + " " + WARNING}},
            "exact_dates_known": {"conf": known},
        }
    )
    assert result["conf"]["bot_notes"] == ("" if known else WARNING)


def test_preserves_user_actions_and_inactive_records(update_travel_warnings):
    entries = {
        "acted": {"status": "open", "user_actioned": True, "bot_notes": WARNING},
        "sent": {"status": "sent", "bot_notes": WARNING},
        "dismissed": {"status": "dismissed", "bot_notes": "Dismissed: off-topic"},
    }
    assert (
        update_travel_warnings.update({"entries": entries, "exact_dates_known": {"acted": True}})
        == entries
    )


def test_known_dates_preserve_absent_notes(update_travel_warnings):
    record = {"status": "open"}
    assert update_travel_warnings.update(
        {"entries": {"conf": record}, "exact_dates_known": {"conf": True}}
    ) == {"conf": record}


def test_whitespace_does_not_accumulate_across_runs(update_travel_warnings):
    payload = {
        "entries": {"conf": {"status": "open", "bot_notes": "  Java topics. \n"}},
        "exact_dates_known": {"conf": False},
    }
    first = update_travel_warnings.update(payload)
    assert first["conf"]["bot_notes"] == "Java topics. " + WARNING
    assert update_travel_warnings.update({**payload, "entries": first}) == first


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {},
        {"entries": [], "exact_dates_known": {}},
        {"entries": {"_last_checked": {}}, "exact_dates_known": {}},
        {"entries": {"conf": []}, "exact_dates_known": {}},
        {"entries": {"conf": {"status": []}}, "exact_dates_known": {}},
        {"entries": {}, "exact_dates_known": {"unknown": True}},
        {"entries": {"conf": {"status": "open"}}, "exact_dates_known": {}},
        {"entries": {"conf": {}}, "exact_dates_known": {"conf": "false"}},
        {"entries": {"conf": {}}, "exact_dates_known": {"conf": 1}},
        {
            "entries": {"conf": {"status": "open", "bot_notes": None}},
            "exact_dates_known": {"conf": False},
        },
    ],
)
def test_invalid_input_emits_no_partial_output(
    update_travel_warnings, monkeypatch, capsys, payload
):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert update_travel_warnings.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "fix the working set and date judgments" in output.err


def test_malformed_json_is_actionable(update_travel_warnings, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("{bad"))
    assert update_travel_warnings.main([]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "retry" in output.err


def test_cli_returns_commit_ready_records(update_travel_warnings):
    result = subprocess.run(
        [sys.executable, update_travel_warnings.__file__],
        input=json.dumps(
            {
                "entries": {"conf": {"status": "open", "name": "Conférence"}},
                "exact_dates_known": {"conf": False},
            }
        ),
        text=True,
        capture_output=True,
        check=True,
    )
    assert result.stderr == ""
    assert json.loads(result.stdout) == {
        "conf": {"status": "open", "name": "Conférence", "bot_notes": WARNING}
    }


def test_commit_preserves_user_action_after_warning_preparation(
    update_travel_warnings, commit_state, tmp_path, monkeypatch, capsys
):
    prepared = update_travel_warnings.update(
        {"entries": {"conf": {"status": "open"}}, "exact_dates_known": {"conf": False}}
    )
    user_record = {"status": "sent", "user_actioned": True, "bot_notes": "User decision"}
    state_path = tmp_path / "cfp-state.json"
    state_path.write_text(json.dumps({"conf": user_record}), encoding="utf-8")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(prepared)))
    assert commit_state.main(["--state", str(state_path)]) == 0
    assert json.loads(capsys.readouterr().out)["skipped_user_actioned"] == 1
    assert json.loads(state_path.read_text(encoding="utf-8")) == {"conf": user_record}
