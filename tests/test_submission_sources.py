"""Recorded-structure replays with synthetic identities; no live source calls."""

import importlib
import importlib.util
import io
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/cfp-submissions/scripts"
FIXTURES = ROOT / "tests/fixtures/submissions"


@pytest.fixture
def modules(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    submission_sources = importlib.import_module("submission_sources")

    spec = importlib.util.spec_from_file_location("source_test_owner", SCRIPTS / "submissions.py")
    assert spec is not None and spec.loader is not None
    owner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(owner)
    return submission_sources, owner


@pytest.fixture
def dashboard():
    return json.loads((FIXTURES / "sessionize-current.json").read_text())


@pytest.fixture
def mail():
    return json.loads((FIXTURES / "mail-families.json").read_text())


def test_observed_dashboard_relationships_and_full_owner_roundtrip(modules, dashboard, tmp_path):
    source, owner = modules
    observations, scopes = source.prepare(dashboard)
    assert len(observations) == 51
    assert len({v["identity"]["event"]["id"] for v in observations}) == 16
    assert Counter(v["fact"]["status"] for v in observations) == {
        "accepted": 6,
        "waitlisted": 1,
        "declined": 8,
        "in_evaluation": 36,
    }
    assert all(
        v["fact"]["submitted_on"] is None
        and v["fact"]["co_speakers"] is None
        and v["fact"]["city"] is None
        and v["fact"]["source_at"] is None
        for v in observations
    )
    assert scopes[0]["complete"] and not scopes[0]["gaps"]
    path = tmp_path / "ledger"
    owner.record_many(path, observations, scopes)
    before = path.read_bytes()
    assert not owner.record_many(path, observations, scopes)["changed"]
    assert path.read_bytes() == before
    query = {"start": "2020-01-01", "end": "2020-12-31"}
    current = owner.query(path, query)
    assert len(current["matches"]) == 43
    assert len([v for v in current["matches"] if v["fields"]["status"] == "waitlisted"]) == 1
    assert len(owner.query(path, query, history=True)["matches"]) == 51
    assert not current["coverage"]["complete"]


@pytest.mark.parametrize("damage", ["account", "filter", "route", "tree", "version", "proposal"])
def test_unknown_dashboard_structure_refuses_without_mutation(modules, dashboard, tmp_path, damage):
    source, owner = modules
    nodes = dashboard["capture"]["nodes"]
    if damage == "account":
        next(n for n in nodes if "event-title" in n["classes"].split())["text"] = ""
    elif damage == "filter":
        for n in nodes:
            if n["tag"] == "LI":
                n["classes"] = n["classes"].replace("active", "")
    elif damage == "route":
        dashboard["capture"]["origin"] = "https://other.example"
    elif damage == "tree":
        nodes[10]["parent"] = 11
    elif damage == "version":
        dashboard["capture"]["schema_version"] = True
    else:
        next(n for n in nodes if "/app/speaker/session/" in n.get("href", ""))["href"] += (
            "?secret=example"
        )
    path = tmp_path / "ledger"
    with pytest.raises(source.SourceError):
        observations, scopes = source.prepare(dashboard)
        owner.record_many(path, observations, scopes)
    assert not path.exists()


@pytest.mark.parametrize(
    "damage", ["events", "sessions", "pagination", "status", "dates", "two-statuses"]
)
def test_partial_page_never_becomes_complete_coverage(modules, dashboard, damage):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    if damage == "events":
        next(n for n in nodes if n["text"] == "(16)")["text"] = "(17)"
    elif damage == "sessions":
        next(n for n in nodes if n["text"] == "1/1 accepted")["text"] = "1/2 accepted"
    elif damage == "pagination":
        nodes[-1]["classes"] = "pagination"
    elif damage == "status":
        next(n for n in nodes if "session-status_0" in n["classes"])["text"] = "Mystery"
    elif damage == "dates":
        next(n for n in nodes if n["text"] == "2 Nov - 4 Nov 2020")["text"] = "date unknown"
    else:
        nodes[143]["classes"] = "session-status_999"
        nodes[143]["text"] = "Mystery"
    observations, scopes = source.prepare(dashboard)
    assert len(observations) == 51 and not scopes[0]["complete"] and scopes[0]["gaps"]
    if damage in {"status", "two-statuses"}:
        assert observations[0]["fact"]["status"] is None
    if damage == "dates":
        assert observations[0]["fact"]["dates"] is None


def test_duplicate_native_event_refuses_instead_of_coalescing_groups(modules, dashboard):
    source, _ = modules
    links = [
        n
        for n in dashboard["capture"]["nodes"]
        if "/app/speaker/event/details/" in n.get("href", "")
    ]
    links[1]["href"] = links[0]["href"]
    with pytest.raises(source.SourceError, match="capture_duplicate_event"):
        source.prepare(dashboard)


def test_mail_real_template_families_unknowns_and_cross_family_dedup(modules, mail, tmp_path):
    source, owner = modules
    observations, scopes = source.prepare(mail)
    assert len(observations) == 15  # 16 source occurrences include one shared actual message.
    assert all(v["identity"] is None for v in observations)
    assert Counter(v["fact"]["status"] for v in observations) == {
        "submitted": 6,
        "declined": 4,
        "accepted": 1,
        "waitlisted": 1,
        None: 3,
    }
    assert all(v["fact"]["submitted_on"] is None for v in observations)
    assert all(v["fact"]["source_at"] == "2020-11-01T00:00:00Z" for v in observations)
    assert all(not v["complete"] for v in scopes)
    # Three identical Oxford confirmations are evidence, never three identified proposals.
    oxford = [
        v
        for v in observations
        if v["fact"]["event_name"] == "Example Event 2020" and v["fact"]["status"] == "submitted"
    ]
    assert len(oxford) == 3 and all(v["fact"]["talk_title"] is None for v in oxford)
    path = tmp_path / "ledger"
    owner.record_many(path, observations, scopes)
    result = owner.query(path, {"start": "2020-01-01", "end": "2020-12-31"})
    assert not result["matches"] and len(result["unresolved"]) == 15
    assert not result["coverage"]["complete"]


def test_mail_bearer_links_and_unrelated_contents_never_reach_facts(modules, mail):
    source, _ = modules
    message = mail["collections"][2]["evidence"]["threads"][0]["messages"][0]
    message["body"] += (
        ' https://example.invalid/control/PRIVATE-BEARER?token=SECRET#private <a href="https://secret.invalid">UNRELATED-ABSTRACT</a>'
    )
    mail["collections"][5]["evidence"]["threads"][0]["messages"][0]["body"] = message["body"]
    observations, _ = source.prepare(mail)
    result = json.dumps(observations)
    assert all(
        s not in result
        for s in ["PRIVATE-BEARER", "SECRET", "UNRELATED-ABSTRACT", "secret.invalid"]
    )
    assert all(v["fact"]["citation"]["url"] is None for v in observations)


def test_incomplete_body_cannot_assert_submission_and_changed_duplicate_holds(modules, mail):
    source, _ = modules
    message = mail["collections"][2]["evidence"]["threads"][0]["messages"][0]
    message["body_complete"] = False
    # The same message appears in the DevOpsDays search: contradictory input must refuse.
    with pytest.raises(source.SourceError, match="mail_identity_conflict"):
        source.prepare(mail)
    duplicate = mail["collections"][5]["evidence"]["threads"][0]["messages"][0]
    duplicate["body_complete"] = False
    observations, scopes = source.prepare(mail)
    receipt = next(v for v in observations if v["fact"]["citation"]["id"] == "example-pretalx")
    assert not receipt["fact"]["complete"]
    assert any("message body incomplete" in s["gaps"] for s in scopes)


def test_whole_import_validates_before_any_write_and_actual_cli(
    modules, dashboard, tmp_path, monkeypatch, capsys
):
    source, owner = modules
    observations, scopes = source.prepare(dashboard)
    path = tmp_path / "ledger"
    broken = deepcopy(observations)
    broken[-1]["fact"]["status"] = "invented"
    with pytest.raises(owner.Hold, match="invalid_status"):
        owner.record_many(path, broken, scopes)
    assert not path.exists()
    monkeypatch.setattr(owner, "LEDGER", path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(dashboard)))
    assert owner.main(["import"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observations"] == result["identities_resolved"] == 51
    assert path.stat().st_mode & 0o777 == 0o600


def test_composed_import_explicit_reference_and_guarded_existing_cfp_projection(
    modules, dashboard, tmp_path
):
    source, owner = modules
    observations, scopes = source.prepare(dashboard)
    path, cfp = tmp_path / "ledger", tmp_path / "cfp-state.json"
    owner.record_many(path, observations, scopes)
    first = observations[0]
    reference = deepcopy(first)
    reference["fact"].update(
        evidence_id="owner-exact-reference-1",
        kind="owner",
        status=None,
        cfp_url="https://example.invalid/cfp-2020",
        citation={"kind": "owner", "id": "owner-note-1", "url": None},
    )
    owner.record(path, reference, manual=True)
    prior = {
        "schema_version": 1,
        "status": "dismissed",
        "user_actioned": False,
        "cfp_url": "https://example.invalid/cfp-2020",
        "bot_notes": "preserve reason",
    }
    state = {"example": prior, "_last_checked": "unchanged", "unrelated": {"keep": True}}
    cfp.write_text(json.dumps(state))
    result = owner.sync(path, cfp)
    assert not result["complete"]  # Other active proposals still lack exact public references.
    state["example"]["status"] = "sent"
    assert json.loads(cfp.read_text()) == state
    assert any(r["outcome"] == "sent" for r in result["results"])
    assert any(r["outcome"] == "unmatched_reference" for r in result["results"])
    # Current immutable owner decision cannot be overridden by imported submission evidence.
    state["example"].update(status="dismissed", user_actioned=True)
    cfp.write_text(json.dumps(state))
    before = cfp.read_bytes()
    result = owner.sync(path, cfp)
    assert not result["complete"] and cfp.read_bytes() == before
    assert any(r["outcome"] == "user_actioned_conflict" for r in result["results"])


def test_owner_manual_no_mail_evidence_retains_multiple_talks_events_and_speakers(
    modules, tmp_path
):
    source, owner = modules
    path = tmp_path / "ledger"
    requests = []
    for event in range(4):
        for talk in range(2):
            fact = source.new_fact(
                "owner",
                f"owner-note-1:{event}:{talk}",
                "2020-12-01T00:00:00Z",
                source_at="2020-11-01T00:00:00Z",
                event_name=f"Manual Event {event}",
                talk_title=f"Manual Talk {talk}",
                status="submitted",
                co_speakers=["Example Co-speaker"],
            )
            fact["citation"]["id"] = "owner-note-1"
            requests.append(
                {
                    "identity": {
                        "event": {"namespace": "owner", "id": f"note-1:event-{event}"},
                        "talk": {"namespace": "owner", "id": f"note-1:talk-{talk}"},
                    },
                    "fact": fact,
                }
            )
    owner.record_many(path, requests, manual=True)
    result = owner.query(path, {"start": "2020-01-01", "end": "2020-12-31"})
    assert len(result["unresolved"]) == 8 and not result["matches"]
    assert all(
        r["fields"]["submitted_on"] is None
        and r["fields"]["co_speakers"] == ["Example Co-speaker"]
        and r["field_state"]["dates"] == "unknown"
        for r in result["unresolved"]
    )


def test_duplicate_mail_parses_known_receipt_before_dedup_in_either_collection_order(modules, mail):
    source, _ = modules
    specific = deepcopy(mail["collections"][-1])
    specific["evidence"]["threads"] = [specific["evidence"]["threads"][1]]
    specific["evidence"].update(complete=True, next_page_token=None, threads_omitted=0, errors=[])
    broad = deepcopy(specific)
    broad["family"] = "cfpdev"
    broad["scope"] = "broader candidate query"
    broad["observed_at"] = "2020-12-01T00:30:00+02:00"
    first = source.prepare(
        {"schema_version": 1, "source": "mail", "collections": [broad, specific]}
    )
    second = source.prepare(
        {"schema_version": 1, "source": "mail", "collections": [specific, broad]}
    )
    assert first[0] == second[0] and len(first[0]) == 1
    assert first[0][0]["fact"]["status"] == "accepted"
    assert first[0][0]["fact"]["talk_title"] == "Example Talk"
    assert first[0][0]["fact"]["observed_at"] == broad["observed_at"]
    for _, scopes in [first, second]:
        assert len(scopes) == 2 and all(not scope["complete"] for scope in scopes)
        assert all(
            "proposal identity unverified; explicit owner/native binding required" in scope["gaps"]
            for scope in scopes
        )


def test_oxford_dates_city_remain_mail_claims_without_official_authority(modules, mail, tmp_path):
    source, owner = modules
    observations, _ = source.prepare(mail)
    claim = next(
        v
        for v in observations
        if v["fact"]["event_name"] == "Example Event 2020" and v["fact"]["status"] == "submitted"
    )
    assert claim["fact"]["dates"] == {"start": "2020-04-13", "end": "2020-04-15"}
    assert claim["fact"]["city"] == "Example City"
    claim["identity"] = {
        "event": {"namespace": "owner", "id": "confirmed-event"},
        "talk": {"namespace": "owner", "id": "confirmed-talk"},
    }
    path = tmp_path / "ledger"
    owner.record(path, claim)
    row = owner.query(path, {"start": "2020-01-01", "end": "2020-12-31"})["unresolved"][0]
    assert row["field_state"]["dates"] == row["field_state"]["city"] == "unknown"


def test_exact_own_form_date_clause_is_retained_without_guessing_city(modules, mail):
    source, _ = modules
    observations, _ = source.prepare(mail)
    own_forms = [
        v for v in observations if v["fact"]["event_name"] == "Example Event 2020 25-26 May"
    ]
    assert len(own_forms) == 2
    assert all(
        v["fact"]["dates"] == {"start": "2020-05-25", "end": "2020-05-26"}
        and v["fact"]["city"] is None
        for v in own_forms
    )
