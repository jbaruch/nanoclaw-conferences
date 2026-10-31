"""Format-independent owner controls; these are not authenticated DOM/mail samples."""

import errno
import importlib.util
import io
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "skills/cfp-submissions/scripts/submissions.py"


@pytest.fixture
def ledger(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPT.parent))
    spec = importlib.util.spec_from_file_location("submission_owner", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def observation():
    return {
        "identity": {
            "event": {"namespace": "sessionize", "id": "event-17"},
            "talk": {"namespace": "sessionize", "id": "proposal-9"},
        },
        "fact": {
            "schema_version": 1,
            "evidence_id": "observed-source-1",
            "kind": "sessionize",
            "citation": {"kind": "sessionize", "id": "proposal-9", "url": None},
            "observed_at": "2020-01-02T00:00:00Z",
            "source_at": "2020-01-01T00:00:00Z",
            "complete": True,
            "event_name": "Example Conference",
            "talk_title": "Example Talk",
            "status": "submitted",
            "submitted_on": "2019-12-31",
            "dates": {"start": "2020-01-05", "end": "2020-01-07"},
            "city": "Example City",
            "co_speakers": ["Example Speaker"],
            "cfp_url": "https://sessionize.com/example-event",
        },
    }


def write(path, value, mode=0o600):
    path.write_text(json.dumps(value))
    path.chmod(mode)


def lookup(ledger, path, start="2020-01-05", end="2020-01-05"):
    return ledger.query(path, {"start": start, "end": end})


def test_first_record_private_idempotence_and_all_source_fields(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    first = ledger.record(path, observation)
    before = path.read_bytes()
    assert first["changed"] and first["identity_resolved"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert ledger.read(path)["rows"][ledger.key(observation["identity"])]["facts"] == {
        observation["fact"]["evidence_id"]: observation["fact"]
    }
    assert not ledger.record(path, observation)["changed"]
    assert path.read_bytes() == before
    result = lookup(ledger, path)
    assert len(result["matches"]) == 1 and not result["coverage"]["complete"]
    assert result["matches"][0]["fields"]["co_speakers"] == ["Example Speaker"]
    assert result["matches"][0]["citations"]["status"][0]["source_at"] == "2020-01-01T00:00:00Z"


def test_same_title_different_native_talk_and_event_are_distinct(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    for n in range(3):
        candidate = deepcopy(observation)
        candidate["fact"]["evidence_id"] = f"source-{n}"
        candidate["identity"]["talk"]["id"] = f"proposal-{n}"
        if n == 2:
            candidate["identity"]["event"]["id"] = "another-event"
        ledger.record(path, candidate)
    assert len(lookup(ledger, path)["matches"]) == 3


def test_unknown_identity_requires_explicit_retained_owner_binding(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    identity = deepcopy(observation["identity"])
    observation["identity"] = None
    ledger.record(path, observation)
    assert not lookup(ledger, path)["matches"]
    assert len(lookup(ledger, path)["unresolved"]) == 1
    # Neither a similar title nor another record command may silently join it.
    observation["identity"] = identity
    before = path.read_bytes()
    with pytest.raises(ledger.Hold, match="evidence_conflict"):
        ledger.record(path, observation)
    assert path.read_bytes() == before
    binding = {
        "evidence_id": observation["fact"]["evidence_id"],
        "identity": identity,
        "owner_binding": {
            "schema_version": 1,
            "owner_message_id": "owner-note-6",
            "observed_at": "2020-01-02T00:00:00Z",
        },
    }
    assert ledger.bind(path, binding)["changed"]
    assert not ledger.bind(path, binding)["changed"]
    result = lookup(ledger, path)
    assert len(result["matches"]) == 1 and not result["unresolved"]
    assert result["matches"][0]["owner_bindings"]["observed-source-1"] == binding["owner_binding"]
    binding["identity"]["talk"]["id"] = "wrong-proposal"
    with pytest.raises(ledger.Hold, match="binding_missing"):
        ledger.bind(path, binding)


def test_identical_unbound_source_replay_preserves_owner_binding(ledger, observation, tmp_path):
    path = tmp_path / "ledger"
    resolved = deepcopy(observation["identity"])
    observation["identity"] = None
    ledger.record(path, observation)
    binding = {
        "evidence_id": observation["fact"]["evidence_id"],
        "identity": resolved,
        "owner_binding": {
            "schema_version": 1,
            "owner_message_id": "owner-note-6",
            "observed_at": "2020-01-02T00:00:00Z",
        },
    }
    ledger.bind(path, binding)
    before = path.read_bytes()
    result = ledger.record(path, observation)
    assert not result["changed"] and result["identity_resolved"]
    assert path.read_bytes() == before
    assert lookup(ledger, path)["matches"][0]["owner_bindings"] == {
        observation["fact"]["evidence_id"]: binding["owner_binding"]
    }
    changed = deepcopy(observation)
    changed["fact"]["status"] = "declined"
    with pytest.raises(ledger.Hold, match="evidence_conflict"):
        ledger.record(path, changed)
    changed = deepcopy(observation)
    changed["identity"] = deepcopy(resolved)
    changed["identity"]["talk"]["id"] = "different-proposal"
    with pytest.raises(ledger.Hold, match="evidence_conflict"):
        ledger.record(path, changed)
    assert path.read_bytes() == before


@pytest.mark.parametrize("boundary", ["2020-01-05", "2020-01-07"])
def test_inclusive_multiday_boundary(ledger, observation, tmp_path, boundary):
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    assert len(lookup(ledger, path, boundary, boundary)["matches"]) == 1
    assert not lookup(ledger, path, "2020-01-08", "2020-01-09")["matches"]


@pytest.mark.parametrize("field", ["dates", "status"])
def test_unknowns_are_not_eliminated_by_range(ledger, observation, tmp_path, field):
    observation["fact"][field] = None
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    result = lookup(ledger, path, "2021-01-01", "2021-01-02")
    assert not result["matches"] and len(result["unresolved"]) == 1
    assert not result["coverage"]["complete"]


def test_partial_fact_kept_without_positive_projection(ledger, observation, tmp_path):
    observation["fact"]["complete"] = False
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    result = lookup(ledger, path)
    assert len(result["unresolved"]) == 1 and not result["matches"]
    assert (
        ledger.read(path)["rows"][ledger.key(observation["identity"])]["facts"]["observed-source-1"]
        == observation["fact"]
    )


def test_dates_require_event_source_not_old_mail(ledger, observation, tmp_path):
    observation["fact"]["kind"] = "mail"
    observation["fact"]["citation"]["kind"] = "mail"
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    assert lookup(ledger, path)["unresolved"][0]["field_state"]["dates"] == "unknown"
    current = deepcopy(observation)
    current["fact"].update(
        evidence_id="official-event-2",
        kind="event_official",
        status=None,
        submitted_on=None,
        co_speakers=None,
        dates={"start": "2020-02-05", "end": "2020-02-07"},
    )
    current["fact"]["citation"]["kind"] = "event_official"
    ledger.record(path, current)
    result = lookup(ledger, path, "2020-02-06", "2020-02-06")
    assert (
        len(result["matches"]) == 1
        and result["matches"][0]["fields"]["dates"] == current["fact"]["dates"]
    )


@pytest.mark.parametrize("missing_clock", [False, True])
def test_incomparable_or_tied_status_conflict_stays_unknown(
    ledger, observation, tmp_path, missing_clock
):
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    second = deepcopy(observation)
    second["fact"].update(evidence_id="source-2", status="declined")
    if missing_clock:
        second["fact"]["source_at"] = None
    ledger.record(path, second)
    result = lookup(ledger, path)
    assert result["unresolved"][0]["field_state"]["status"] == "conflict"
    assert len(result["unresolved"][0]["citations"]["status"]) == 2


def test_newer_identified_decision_keeps_original_fact(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    second = deepcopy(observation)
    second["fact"].update(
        evidence_id="decision-2", source_at="2020-01-01T12:00:00Z", status="accepted"
    )
    ledger.record(path, second)
    result = lookup(ledger, path)
    assert result["matches"][0]["fields"]["status"] == "accepted"
    assert len(ledger.read(path)["rows"][ledger.key(observation["identity"])]["facts"]) == 2


def test_disappearance_cannot_withdraw_a_talk(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    ledger.record(path, observation)
    candidate = deepcopy(observation)
    candidate["identity"]["talk"]["id"] = "new-talk"
    candidate["fact"]["evidence_id"] = "source-new"
    candidate["coverage"] = {
        "schema_version": 1,
        "id": "current-page",
        "source": "sessionize",
        "scope": "current visible page only",
        "checked_at": "2020-01-02T00:00:00Z",
        "complete": True,
        "gaps": [],
    }
    ledger.record(path, candidate)
    assert len(lookup(ledger, path)["matches"]) == 2
    assert not lookup(ledger, path)["coverage"]["complete"]


def test_older_scope_cannot_replace_newer_coverage(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    observation["coverage"] = {
        "schema_version": 1,
        "id": "scope-1",
        "source": "mail",
        "scope": "bounded query",
        "checked_at": "2020-01-02T00:00:00Z",
        "complete": False,
        "gaps": ["next page unconsumed"],
    }
    ledger.record(path, observation)
    before = path.read_bytes()
    observation["coverage"]["checked_at"] = "2020-01-01T00:00:00Z"
    with pytest.raises(ledger.Hold, match="coverage_conflict"):
        ledger.record(path, observation)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "damaged",
    [None, [], {"schema_version": True}, {"schema_version": 2}, {"schema_version": 1, "rows": {}}],
)
def test_retained_bad_or_future_never_reset(ledger, observation, tmp_path, damaged):
    path = tmp_path / "ledger.json"
    write(path, damaged)
    before = path.read_bytes()
    with pytest.raises(ledger.Hold):
        ledger.record(path, observation)
    assert path.read_bytes() == before


def test_duplicate_fields_and_malformed_retained_bytes_hold(ledger, observation, tmp_path):
    path = tmp_path / "ledger.json"
    for raw in (b'{"schema_version":2,"schema_version":1}', b"{bad"):
        path.write_bytes(raw)
        path.chmod(0o600)
        with pytest.raises(ledger.Hold):
            ledger.record(path, observation)
        assert path.read_bytes() == raw


def test_unknown_query_never_creates_store_or_proves_absence(ledger, tmp_path):
    path = tmp_path / "absent.json"
    result = lookup(ledger, path)
    assert not path.exists() and not result["matches"]
    assert not result["coverage"]["complete"]
    assert "uninitialized" in result["coverage"]["reason"]


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_aliases_rejected_without_target_change(ledger, observation, tmp_path, alias):
    original, path = tmp_path / "original", tmp_path / "ledger"
    write(original, ledger.empty())
    if alias == "symlink":
        path.symlink_to(original)
    else:
        os.link(original, path)
    before = original.read_bytes()
    with pytest.raises(ledger.Hold, match="unsafe_file"):
        ledger.record(path, observation)
    assert original.read_bytes() == before


def test_private_modes_and_capacity_hold(ledger, observation, tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    write(path, ledger.empty(), mode=0o644)
    with pytest.raises(ledger.Hold, match="private_file"):
        ledger.record(path, observation)
    path.chmod(0o600)
    before = path.read_bytes()
    monkeypatch.setattr(ledger, "MAX_ROWS", 0)
    with pytest.raises(ledger.Hold, match="ledger_shape"):
        ledger.record(path, observation)
    assert path.read_bytes() == before


def test_actual_flock_prevents_competing_write(ledger, observation, tmp_path):
    path = tmp_path / "ledger"
    with ledger.locked(path), pytest.raises(ledger.Hold, match="ledger_busy"):
        ledger.record(path, observation)
    assert not path.exists()
    ledger.record(path, observation)
    assert len(lookup(ledger, path)["matches"]) == 1


def test_manual_cannot_label_a_provider_receipt_as_owner(ledger, observation, tmp_path):
    path = tmp_path / "ledger"
    with pytest.raises(ledger.Hold, match="manual_source"):
        ledger.record(path, observation, manual=True)
    assert not path.exists()
    observation["fact"]["kind"] = "owner"
    observation["fact"]["citation"]["kind"] = "owner"
    ledger.record(path, observation, manual=True)
    # Owner-declared dates still do not become event-source verification.
    assert lookup(ledger, path)["unresolved"][0]["field_state"]["dates"] == "unknown"


@pytest.fixture
def cfp(observation):
    return {
        "schema_version": 1,
        "status": "dismissed",
        "cfp_url": observation["fact"]["cfp_url"],
        "bot_notes": "Keep this original reason",
        "user_actioned": False,
        "unrelated": {"old": "facts"},
    }


def test_composed_real_cfp_owner_changes_only_sent(ledger, observation, cfp, tmp_path):
    path, old = tmp_path / "ledger", tmp_path / "cfp-state.json"
    ledger.record(path, observation)
    state = {
        "event": cfp,
        "other": {"schema_version": 1, "status": "remind"},
        "_last_checked": "preserved",
    }
    write(old, state, mode=0o640)
    result = ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert result["outcome"] == "sent"
    state["event"]["status"] = "sent"
    assert json.loads(old.read_text()) == state
    assert old.stat().st_mode & 0o777 == 0o640
    assert (
        ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})[
            "outcome"
        ]
        == "already_sent"
    )
    assert not list(tmp_path.glob("*cursor*"))


def test_cfp_user_actioned_is_preserved(ledger, observation, cfp, tmp_path):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    cfp["user_actioned"] = True
    write(old, {"event": cfp})
    before = old.read_bytes()
    result = ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert result["outcome"] == "user_actioned_conflict" and old.read_bytes() == before


@pytest.mark.parametrize("edit", ["user_actioned", "note"])
def test_actual_owner_cas_retains_concurrent_changes(
    ledger, observation, cfp, tmp_path, monkeypatch, edit
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    write(old, {"event": cfp})
    original = ledger.owner_call

    def race(owner, arguments, value):
        result = original(owner, arguments, value)
        if arguments == ["--snapshot"]:
            changed = json.loads(old.read_text())
            if edit == "user_actioned":
                changed["event"]["user_actioned"] = True
            else:
                changed["event"]["bot_notes"] = "new owner note"
            write(old, changed)
        return result

    monkeypatch.setattr(ledger, "owner_call", race)
    result = ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert result["outcome"] == "conflict"
    assert json.loads(old.read_text())["event"]["status"] == "dismissed"


@pytest.mark.parametrize(
    "field,value", [("schema_version", 2), ("cfp_url", "https://sessionize.com/another")]
)
def test_cfp_future_and_wrong_identity_never_written(
    ledger, observation, cfp, tmp_path, field, value
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    cfp[field] = value
    write(old, {"event": cfp})
    before = old.read_bytes()
    with pytest.raises(ledger.Hold):
        ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert old.read_bytes() == before


def test_actual_cli_result_and_fixed_error_contract(
    ledger, observation, tmp_path, monkeypatch, capsys
):
    monkeypatch.setattr(ledger, "LEDGER", tmp_path / "ledger")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(observation)))
    assert ledger.main(["record"]) == 0
    assert json.loads(capsys.readouterr().out)["recorded"]
    monkeypatch.setattr("sys.stdin", io.StringIO('{"schema_version":1,"schema_version":2}'))
    assert ledger.main(["record"]) == 1
    result = capsys.readouterr()
    assert not result.out and "duplicate_field" in result.err


def test_unknown_labels_and_submission_clock_remain_unknown(ledger, observation, tmp_path):
    path = tmp_path / "ledger"
    observation["fact"].update(event_name=None, talk_title=None, submitted_on=None)
    ledger.record(path, observation)
    item = lookup(ledger, path)["matches"][0]
    assert item["fields"]["submitted_on"] is None
    assert item["field_state"]["talk_title"] == "unknown"


def test_existing_disappearance_is_not_first_run(ledger, observation, tmp_path, monkeypatch):
    path = tmp_path / "ledger"
    ledger.record(path, observation)

    def disappeared(*args, **kwargs):
        raise FileNotFoundError()

    monkeypatch.setattr(ledger, "file_bytes", disappeared)
    with pytest.raises(ledger.Hold, match="changed_file"):
        ledger.read(path)


def test_failed_replace_preserves_previous_bytes_and_cleans_temporary(
    ledger, observation, tmp_path, monkeypatch
):
    path = tmp_path / "ledger"
    ledger.record(path, observation)
    before = path.read_bytes()
    observation["fact"]["evidence_id"] = "another-observation"
    error = PermissionError(errno.EACCES, "fixture denial")

    def denied(*args):
        raise error

    monkeypatch.setattr(ledger.os, "replace", denied)
    with pytest.raises(PermissionError) as held:
        ledger.record(path, observation)
    assert held.value is error and path.read_bytes() == before
    assert not list(tmp_path.glob(".cfp-submissions-*"))


def test_current_inner_corruption_holds_instead_of_replacing(ledger, observation, tmp_path):
    path = tmp_path / "ledger"
    ledger.record(path, observation)
    damaged = json.loads(path.read_text())
    row = next(iter(damaged["rows"].values()))
    row["facts"]["observed-source-1"]["status"] = ["accepted"]
    write(path, damaged)
    before = path.read_bytes()
    with pytest.raises(ledger.Hold, match="invalid_status"):
        ledger.record(path, observation)
    assert path.read_bytes() == before


def test_official_event_page_cannot_assert_owner_acceptance(ledger, observation, tmp_path):
    observation["fact"]["kind"] = "event_official"
    observation["fact"]["citation"]["kind"] = "event_official"
    with pytest.raises(ledger.Hold, match="event_scope"):
        ledger.record(tmp_path / "ledger", observation)


def test_main_output_bound_is_a_hold_not_truncated_success(
    ledger, observation, tmp_path, monkeypatch, capsys
):
    path = tmp_path / "ledger"
    ledger.record(path, observation)
    before = path.read_bytes()
    monkeypatch.setattr(ledger, "LEDGER", path)
    monkeypatch.setattr(ledger, "MAX_OUTPUT", 10)
    monkeypatch.setattr("sys.stdin", io.StringIO('{"start":"2020-01-05","end":"2020-01-07"}'))
    assert ledger.main(["query"]) == 1
    result = capsys.readouterr()
    assert not result.out and "query_capacity" in result.err and path.read_bytes() == before


def test_wrong_neighbor_without_manifest_is_not_cfp_owner(ledger, tmp_path, monkeypatch):
    own = tmp_path / "skills/cfp-submissions/scripts/submissions.py"
    own.parent.mkdir(parents=True)
    own.touch()
    neighbor = tmp_path / "skills/check-cfps/scripts/commit-state.py"
    neighbor.parent.mkdir(parents=True)
    neighbor.write_text("raise AssertionError('must not execute')\n")
    monkeypatch.setattr(ledger, "OWNER", tmp_path / "absent-native-owner")
    monkeypatch.setattr(ledger, "__file__", str(own))
    with pytest.raises(ledger.Hold, match="cfp_owner_unavailable"):
        ledger.cfp_owner()


def test_reconciliation_private_invocation_normalizes_inherited_modes_before_bytes(
    ledger, observation, cfp, tmp_path, monkeypatch
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    write(old, {"event": cfp})
    original_open = ledger.os.open
    original_call = ledger.owner_call

    def inherited_open(target, flags, mode=0o777, **kwargs):
        fd = original_open(target, flags, mode, **kwargs)
        if str(target).endswith("expected.json"):
            ledger.os.fchmod(fd, 0o777)
        return fd

    def inspect_call(owner, arguments, value):
        if "--expected-digests" in arguments:
            expected = Path(arguments[arguments.index("--expected-digests") + 1])
            assert expected.stat().st_mode & 0o777 == 0o600
            assert expected.parent.stat().st_mode & 0o777 == 0o700
        return original_call(owner, arguments, value)

    monkeypatch.setattr(ledger.os, "open", inherited_open)
    monkeypatch.setattr(ledger, "owner_call", inspect_call)
    assert (
        ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})[
            "outcome"
        ]
        == "sent"
    )


def test_sync_changed_ledger_before_projection_holds_checked_generation(
    ledger, observation, cfp, tmp_path, monkeypatch
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    before_hash = ledger.digest(ledger.read(path))
    write(old, {"event": cfp})
    original = ledger.file_bytes
    changed = False

    def race(target, private=True):
        nonlocal changed
        data = original(target, private)
        if target == old and not changed:
            changed = True
            unknown = deepcopy(observation)
            unknown["identity"] = None
            unknown["fact"]["evidence_id"] = "new-unbound-source"
            ledger.record(path, unknown)
        return data

    monkeypatch.setattr(ledger, "file_bytes", race)
    result = ledger.sync(path, old)
    assert not result["complete"] and result["generation_changed"]
    assert result["ledger_sha256"] == before_hash
    assert result["current_ledger_sha256"] == ledger.digest(ledger.read(path)) != before_hash
    assert json.loads(old.read_text())["event"]["status"] == "dismissed"


def test_sync_final_generation_change_reports_incomplete_without_rollback(
    ledger, observation, cfp, tmp_path, monkeypatch
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    write(old, {"event": cfp})
    original = ledger.owner_call

    def race(owner, arguments, value):
        result = original(owner, arguments, value)
        if "--expected-digests" in arguments:
            unknown = deepcopy(observation)
            unknown["identity"] = None
            unknown["fact"]["evidence_id"] = "new-later-source"
            ledger.record(path, unknown)
        return result

    monkeypatch.setattr(ledger, "owner_call", race)
    result = ledger.sync(path, old)
    assert not result["complete"] and result["generation_changed"]
    assert result["ledger_sha256"] != result["current_ledger_sha256"]
    assert result["results"][0]["outcome"] == "sent"
    assert json.loads(old.read_text())["event"]["status"] == "sent"


def test_actual_committer_refuses_new_duplicate_reference_sibling(
    ledger, observation, cfp, tmp_path, monkeypatch
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    write(old, {"event": cfp, "_last_checked": "preserved"})
    original = ledger.owner_call

    def race(owner, arguments, value):
        result = original(owner, arguments, value)
        if arguments == ["--snapshot"]:
            state = json.loads(old.read_text())
            state["new-duplicate"] = {**cfp, "bot_notes": "new sibling retained"}
            write(old, state)
        return result

    monkeypatch.setattr(ledger, "owner_call", race)
    result = ledger.sync(path, old)
    assert not result["complete"] and result["results"][0]["outcome"] == "conflict"
    assert result["results"][0]["owner_result"]["skipped_changed"] == ["event"]
    assert json.loads(old.read_text()) == {
        "event": cfp,
        "new-duplicate": {**cfp, "bot_notes": "new sibling retained"},
        "_last_checked": "preserved",
    }


def test_guarded_reconciliation_never_bootstraps_disappeared_cfp_state(
    ledger, observation, cfp, tmp_path, monkeypatch
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    write(old, {"event": cfp})
    original = ledger.owner_call

    def race(owner, arguments, value):
        result = original(owner, arguments, value)
        if arguments == ["--snapshot"]:
            old.unlink()
        return result

    monkeypatch.setattr(ledger, "owner_call", race)
    result = ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert result["outcome"] == "conflict" and not old.exists()


def test_already_sent_still_requires_unambiguous_current_reference(
    ledger, observation, cfp, tmp_path
):
    path, old = tmp_path / "ledger", tmp_path / "cfp.json"
    ledger.record(path, observation)
    cfp["status"] = "sent"
    write(old, {"event": cfp, "other": dict(cfp)})
    before = old.read_bytes()
    with pytest.raises(ledger.Hold, match="cfp_identity"):
        ledger.reconcile(path, old, {"identity": observation["identity"], "slug": "event"})
    assert old.read_bytes() == before
