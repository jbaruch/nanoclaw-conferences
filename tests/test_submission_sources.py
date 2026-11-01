"""Recorded-structure replays with synthetic identities; no live source calls."""

import importlib
import importlib.util
import io
import json
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from submission_fixture_builders import mail_packet, sessionize_packet

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills/cfp-submissions/scripts"


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
    return sessionize_packet()


@pytest.fixture
def mail():
    return mail_packet()


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


@pytest.mark.parametrize(
    "damage",
    ["account", "filter", "route", "tree", "version", "proposal", "title", "scope", "extra"],
)
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
    elif damage == "proposal":
        next(n for n in nodes if "/app/speaker/session/" in n.get("href", ""))["href"] += (
            "?secret=example"
        )
    elif damage in {"title", "scope"}:
        dashboard["capture"].pop(damage)
    else:
        dashboard["capture"]["future"] = "unsupported"
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
    assert all(v["fact"]["source_at"] == "2020-11-01T00:00:00+00:00" for v in observations)
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
    mail["collections"][2]["evidence"]["complete"] = False
    mail["collections"][2]["evidence"]["threads"][0]["complete"] = False
    # The same message appears in the DevOpsDays search: contradictory input must refuse.
    with pytest.raises(source.SourceError, match="mail_identity_conflict"):
        source.prepare(mail)
    duplicate = mail["collections"][5]["evidence"]["threads"][0]["messages"][0]
    duplicate["body_complete"] = False
    mail["collections"][5]["evidence"]["complete"] = False
    mail["collections"][5]["evidence"]["threads"][0]["complete"] = False
    observations, scopes = source.prepare(mail)
    receipt = next(v for v in observations if v["fact"]["citation"]["id"] == "a004")
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
    assert first[0][0]["fact"]["observed_at"] == specific["observed_at"]
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


def test_unchanged_native_snapshots_over_a_hundred_recaptures_keep_one_fact(
    modules, dashboard, tmp_path
):
    source, owner = modules
    path = tmp_path / "ledger"
    start = datetime(2020, 12, 1, tzinfo=UTC)
    for capture_number in range(101):
        dashboard["observed_at"] = (
            (start + timedelta(seconds=capture_number))
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z")
        )
        observations, _ = source.prepare(dashboard)
        owner.record(path, observations[0])
    row = next(iter(owner.read(path)["rows"].values()))
    assert len(row["facts"]) == 1
    assert next(iter(row["facts"].values()))["observed_at"] == dashboard["observed_at"]
    assert next(iter(row["facts"].values()))["source_at"] is None


def test_native_status_cycle_and_older_replay_preserve_history_without_downgrade(
    modules, dashboard, tmp_path
):
    source, owner = modules
    path = tmp_path / "ledger"
    badge = next(n for n in dashboard["capture"]["nodes"] if "session-status_0" in n["classes"])
    first = source.prepare(dashboard)[0][0]
    owner.record(path, first)
    badge.update(classes="badge session-status_20", text="Declined")
    dashboard["observed_at"] = "2020-12-02T00:00:00.000Z"
    second = source.prepare(dashboard)[0][0]
    owner.record(path, second)
    query = {"start": "2020-01-01", "end": "2020-12-31"}
    assert not owner.query(path, query)["matches"]
    owner.record(path, first)
    assert not owner.query(path, query)["matches"]
    badge.update(classes="badge session-status_0", text="Accepted")
    dashboard["observed_at"] = "2020-12-03T00:00:00.000Z"
    third = source.prepare(dashboard)[0][0]
    owner.record(path, third)
    result = owner.query(path, query)
    assert result["matches"][0]["fields"]["status"] == "accepted"
    row = next(iter(owner.read(path)["rows"].values()))
    assert len(row["facts"]) == 2
    assert first["fact"]["evidence_id"] == third["fact"]["evidence_id"]
    assert result["matches"][0]["checked_at"] == dashboard["observed_at"]


def test_tied_native_snapshots_and_cross_source_conflicts_stay_unresolved(
    modules, dashboard, tmp_path
):
    source, owner = modules
    path = tmp_path / "ledger"
    first = source.prepare(dashboard)[0][0]
    owner.record(path, first)
    badge = next(n for n in dashboard["capture"]["nodes"] if "session-status_0" in n["classes"])
    badge.update(classes="badge session-status_20", text="Declined")
    owner.record(path, source.prepare(dashboard)[0][0])
    query = {"start": "2020-01-01", "end": "2020-12-31"}
    assert owner.query(path, query)["unresolved"][0]["field_state"]["status"] == "conflict"
    badge.update(classes="badge session-status_0", text="Accepted")
    dashboard["observed_at"] = "2020-12-02T00:00:00.000Z"
    owner.record(path, source.prepare(dashboard)[0][0])
    mail_claim = deepcopy(first)
    mail_claim["fact"].update(
        evidence_id="immutable-mail-decision",
        kind="mail",
        status="declined",
        source_at="2020-11-30T00:00:00Z",
        citation={"kind": "mail", "id": "native-mail-id", "url": None},
    )
    owner.record(path, mail_claim)
    result = owner.query(path, query)
    assert result["unresolved"][0]["field_state"]["status"] == "conflict"
    assert any(
        c["citation"]["kind"] == "mail" for c in result["unresolved"][0]["citations"]["status"]
    )


def test_new_unknown_native_status_does_not_reuse_an_old_known_status(modules, dashboard, tmp_path):
    source, owner = modules
    path = tmp_path / "ledger"
    owner.record(path, source.prepare(dashboard)[0][0])
    dashboard["observed_at"] = "2020-12-02T00:00:00.000Z"
    badge = next(n for n in dashboard["capture"]["nodes"] if "session-status_0" in n["classes"])
    badge.update(classes="badge session-status_999", text="Unknown label")
    owner.record(path, source.prepare(dashboard)[0][0])
    result = owner.query(path, {"start": "2020-01-01", "end": "2020-12-31"})
    assert not result["matches"] and result["unresolved"][0]["fields"]["status"] is None


def test_unchanged_mail_recaptures_refresh_only_observation_metadata_and_binding(
    modules, mail, tmp_path
):
    source, owner = modules
    path = tmp_path / "ledger"
    capture = deepcopy(mail["collections"][-1])
    capture["evidence"]["threads"] = [capture["evidence"]["threads"][1]]
    packet = {"schema_version": 1, "source": "mail", "collections": [capture]}
    first = source.prepare(packet)[0][0]
    owner.record(path, first)
    identity = {
        "event": {"namespace": "owner", "id": "explicit-event"},
        "talk": {"namespace": "owner", "id": "explicit-talk"},
    }
    binding = {
        "schema_version": 1,
        "owner_message_id": "owner-note-2",
        "observed_at": "2020-12-01T00:00:00Z",
    }
    owner.bind(
        path,
        {
            "identity": identity,
            "evidence_id": first["fact"]["evidence_id"],
            "owner_binding": binding,
        },
    )
    capture["observed_at"] = "2020-12-03T00:00:00Z"
    later = source.prepare(packet)[0][0]
    assert later["fact"]["evidence_id"] == first["fact"]["evidence_id"]
    owner.record(path, later)
    owner.record(path, first)  # An older replay cannot regress the last observed clock.
    row = owner.read(path)["rows"][owner.key(identity)]
    assert len(row["facts"]) == 1 and row["owner_bindings"] == {
        first["fact"]["evidence_id"]: binding
    }
    retained = next(iter(row["facts"].values()))
    assert retained["observed_at"] == capture["observed_at"]
    assert retained["source_at"] == first["fact"]["source_at"]


@pytest.mark.parametrize("identifier", ["f", "A" * 101, "B" * 128])
def test_maintained_mail_id_bounds_and_long_id_recapture(modules, mail, tmp_path, identifier):
    source, owner = modules
    collection = deepcopy(mail["collections"][-1])
    collection["evidence"].update(
        threads=collection["evidence"]["threads"][:1], complete=False, threads_omitted=2
    )
    collection["evidence"]["threads"][0]["messages"][0]["messageId"] = identifier
    collection["evidence"]["threads"][0]["thread_latest_message_id"] = identifier
    packet = {"schema_version": 1, "source": "mail", "collections": [collection]}
    observations, scopes = source.prepare(packet)
    assert observations[0]["fact"]["citation"]["id"] == identifier
    assert source.adapter_kind(None, observations[0]["fact"]) == "mail"
    path = tmp_path / "ledger"
    owner.record_many(path, observations, scopes)
    collection["observed_at"] = "2020-12-03T00:00:00Z"
    observations, scopes = source.prepare(packet)
    assert owner.record_many(path, observations, scopes)["changed"]
    assert len(owner.read(path)["unresolved"]) == 1


@pytest.mark.parametrize("identifier", ["g", "a_b", "", "a" * 129])
def test_rejects_nonproducer_mail_ids(modules, mail, identifier):
    source, _ = modules
    collection = deepcopy(mail["collections"][-1])
    collection["evidence"].update(
        threads=collection["evidence"]["threads"][:1], complete=False, threads_omitted=2
    )
    collection["evidence"]["threads"][0]["messages"][0]["messageId"] = identifier
    with pytest.raises(source.SourceError):
        source.prepare({"schema_version": 1, "source": "mail", "collections": [collection]})


@pytest.mark.parametrize("template", ["sessionize", "own-form", "oxford", "refused", "accepted"])
@pytest.mark.parametrize("long_field", ["first", "second"])
def test_body_template_overlong_fields_remain_unknown(modules, template, long_field):
    source, _ = modules
    first = "X" * 301 if long_field == "first" else "Example Talk"
    second = "Y" * 301 if long_field == "second" else "Example Event"
    if template == "sessionize":
        subject = "Your proposal: " + first
        body = f"We have received your proposal “{first}” to {second}. We will notify you"
    elif template == "own-form":
        subject = f"{second} We received your proposal: {first}"
        body = f'Thank you for submitting "{first}" to {second}. You can view, edit, or withdraw'
    elif template == "oxford":
        subject = "Thank you for submitting your proposal"
        body = (
            f"Thank you for submitting your proposal to speak at {first} , from {second}. "
            "We will review completed proposals. Oxford Abstracts"
        )
    elif template == "refused":
        subject = f"[example] Your proposal has been refused : {first}"
        body = (
            f'Thank you for submitting your proposal "{first}" for {second}. '
            "your talk was not selected"
        )
    else:
        subject = f"[example] Your proposal has been accepted : {first}"
        body = f'proposal, "{first}", has been accepted for {second}!'
    assert source.mail_fields("pretalx", subject, body) == {}


def test_body_template_exact_field_bound_retains_positive_claim(modules):
    source, _ = modules
    title, event = "T" * 300, "E" * 300
    fields = source.mail_fields(
        "pretalx",
        "Your proposal: " + title,
        f"We have received your proposal “{title}” to {event}. We will notify you",
    )
    assert fields == {"talk_title": title, "event_name": event, "status": "submitted"}


def test_repeated_untrusted_delimiters_cannot_stall_source_import():
    import subprocess
    import sys

    # A separate process bounds a security regression without hanging the suite.
    # This is a generous completion limit, not a timing/throughput benchmark.
    program = r"""
import json, sys
sys.path.insert(0, sys.argv[1])
from submission_sources import mail_fields
cases = [
    ("Your proposal: Example", "We have received your proposal “" + "” to x " * 30000),
    ("Thank you for submitting your proposal",
     "Thank you for submitting your proposal to speak at "
     + " from x " * 25000 + " Oxford Abstracts"),
]
print(json.dumps([mail_fields("pretalx", subject, body) for subject, body in cases]))
"""
    result = subprocess.run(
        [sys.executable, "-c", program, str(SCRIPTS)],
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(result.stdout) == [{}, {}]
    assert not result.stderr


@pytest.mark.parametrize(
    "damage",
    [
        "missing_root_parent",
        "missing_classes",
        "missing_tag",
        "missing_text",
        "extra",
        "tag",
        "text_format",
        "text_variation",
        "text_spacing",
        "text_utf16",
        "class_utf16",
        "href",
        "omitted_false",
        "link_conflict",
    ],
)
def test_complete_host_node_shape_is_required(modules, dashboard, damage):
    source, _ = modules
    if damage.startswith("missing_"):
        key = "parent" if damage == "missing_root_parent" else damage.removeprefix("missing_")
        del dashboard["capture"]["nodes"][0][key]
    else:
        node = {"tag": "A", "parent": 0, "classes": "", "text": "Unrelated link"}
        if damage == "extra":
            node["extra_metadata"] = "not emitted by provider"
        elif damage == "tag":
            node["tag"] = "bad:tag"
        elif damage == "text_format":
            node["text"] = "visible\u200btext"
        elif damage == "text_variation":
            node["text"] = "visible\ufe0f"
        elif damage == "text_spacing":
            node["text"] = " unnormalized "
        elif damage == "text_utf16":
            node["text"] = "😀" * 8001
        elif damage == "class_utf16":
            node["classes"] = "😀" * 501
        elif damage == "href":
            node["href"] = "https://other.invalid/private?token=NOT-EXPORTED"
        elif damage == "omitted_false":
            node["link_omitted"] = False
        else:
            node["href"] = "https://sessionize.com/app/speaker/event/details/424242"
            node["link_omitted"] = True
        dashboard["capture"]["nodes"].append(node)
    with pytest.raises(source.SourceError):
        source.prepare(dashboard)


@pytest.mark.parametrize(
    "tag",
    [
        "SCRIPT",
        "STYLE",
        "NOSCRIPT",
        "FORM",
        "INPUT",
        "TEXTAREA",
        "SELECT",
        "OPTION",
        "IFRAME",
        "OBJECT",
        "EMBED",
    ],
)
@pytest.mark.parametrize("surface", ["event", "proposal", "date", "count", "status"])
def test_excluded_host_nodes_cannot_supply_submission_evidence(
    modules, dashboard, tmp_path, tag, surface
):
    source, owner = modules
    nodes = dashboard["capture"]["nodes"]
    if surface in {"event", "proposal"}:
        route = "/event/details/" if surface == "event" else "/session/"
        anchor = next(node for node in nodes if route in node.get("href", ""))
        # The entire subtree, including its valid identity link, would be absent
        # from the Host capture when its parent is excluded.
        target = nodes[anchor["parent"]]
    elif surface == "date":
        target = next(node for node in nodes if node["text"] == "2 Nov - 4 Nov 2020")
    elif surface == "count":
        target = next(node for node in nodes if node["text"] == "(16)")
    else:
        target = next(node for node in nodes if "session-status_0" in node["classes"])
    target["tag"] = tag.lower() if surface == "date" else tag
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="capture_node_tag"):
        observations, scopes = source.prepare(dashboard)
        owner.record_many(ledger, observations, scopes)
    assert not ledger.exists()


@pytest.mark.parametrize("surface", ["filter", "account", "status"])
def test_every_host_anchor_requires_one_link_projection(modules, dashboard, surface):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    if surface == "filter":
        target = next(
            node for node in nodes if node["text"] == "Current events" and node["tag"] == "A"
        )
    elif surface == "account":
        target = next(node for node in nodes if "event-title" in node["classes"].split())
    else:
        target = next(node for node in nodes if "session-status_0" in node["classes"])
    target["tag"] = "A"
    target.pop("href", None)
    target.pop("link_omitted", None)
    with pytest.raises(source.SourceError, match="capture_node_link"):
        source.prepare(dashboard)


@pytest.mark.parametrize("surface", ["status", "event", "proposal"])
def test_host_preorder_cannot_reopen_a_closed_subtree(modules, dashboard, surface):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    if surface == "status":
        target = next(node for node in nodes if "session-status_0" in node["classes"])
    else:
        route = "/event/details/" if surface == "event" else "/session/"
        target = next(node for node in nodes if route in node.get("href", ""))
    # Returning to BODY closes every intervening subtree. Its next native
    # sibling cannot then claim the earlier table/row as its parent.
    target["parent"] = 0
    with pytest.raises(source.SourceError, match="capture_tree"):
        source.prepare(dashboard)


def test_host_svg_shape_and_omitted_link_metadata_remain_supported(modules, dashboard):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    nodes.extend(
        [
            {"tag": "svg", "parent": 0, "classes": "icon", "text": ""},
            {"tag": "path", "parent": len(nodes), "classes": "", "text": ""},
            {"tag": "A", "parent": 0, "classes": "", "text": "Other", "link_omitted": True},
        ]
    )
    observations, _ = source.prepare(dashboard)
    assert len(observations) == 51


def test_cli_first_import_initializes_one_private_ledger(
    modules, dashboard, tmp_path, monkeypatch, capsys
):
    _, owner = modules
    path = tmp_path / "ledger"
    monkeypatch.setattr(owner, "LEDGER", path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(dashboard)))
    assert not path.exists() and owner.main(["import"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["observations"] == 51 and len(owner.read(path)["rows"]) == 51
    assert path.stat().st_mode & 0o777 == 0o600


def test_native_utf16_title_limit_is_not_a_python_codepoint_limit(modules, dashboard):
    source, _ = modules
    dashboard["capture"]["title"] = "😀" * 151
    with pytest.raises(source.SourceError):
        source.prepare(dashboard)


def test_native_node_count_bound_is_preserved(modules, dashboard):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    nodes.extend(
        {"tag": "SPAN", "parent": 0, "classes": "", "text": ""} for _ in range(12001 - len(nodes))
    )
    with pytest.raises(source.SourceError, match="capture_bounds"):
        source.prepare(dashboard)


def test_supported_assigned_unicode_and_native_exact_utf16_bound(modules, dashboard):
    source, _ = modules
    dashboard["capture"]["title"] = "😀" * 150
    dashboard["capture"]["nodes"].append(
        {"tag": "SPAN", "parent": 0, "classes": "", "text": "😀" * 8000}
    )
    observations, _ = source.prepare(dashboard)
    assert len(observations) == 51


def hydrated_mail(*, partial=False, missing=False):
    """Synthetic actual-format wrapper; no provider or retained mail is read."""
    message = {
        "messageId": "a001",
        "from": "source@example.invalid",
        "to": "owner@example.invalid",
        "subject": "Your proposal: Example Talk",
        "internalDate": "2020-11-01T00:00:00+00:00",
        "labelIds": [],
        "body": "Incomplete prefix",
        "body_complete": False,
    }
    full = {
        "schema_version": 1,
        "message_id": "a001",
        "thread_id": "b001",
        "subject": message["subject"],
        "from": message["from"],
        "body": (
            'We have received your proposal "Example Talk" to Example Event. We will notify you'
        ),
        "body_complete": not partial,
        "citation_url": "https://mail.google.com/mail/u/0/#all/b001",
        "source_trust": "untrusted",
    }
    wrapper = {
        "schema_version": 1,
        "search": {
            "schema_version": 1,
            "threads": [
                {
                    "thread_id": "b001",
                    "citation_url": "https://mail.google.com/mail/u/0/#all/b001",
                    "source_trust": "untrusted",
                    "message_count": 1,
                    "thread_latest_message_id": message["messageId"],
                    "thread_latest_message_date": message["internalDate"],
                    "thread_latest_message_from": "them",
                    "messages": [message],
                    "messages_omitted": 0,
                    "complete": False,
                }
            ],
            "next_page_token": None,
            "threads_omitted": 0,
            "complete": False,
            "errors": [],
        },
        "body_reads": []
        if missing
        else [
            {
                "message_id": "a001",
                "thread_id": "b001",
                "internalDate": message["internalDate"],
                "evidence": full,
            }
        ],
        "body_reads_omitted": int(missing),
        "complete": False,
        "errors": (
            [{"message_id": "a001", "kind": "body_unavailable"}]
            if missing
            else [{"message_id": "a001", "kind": "body_incomplete"}]
            if partial
            else []
        ),
    }
    return {
        "schema_version": 2,
        "source": "mail",
        "collections": [
            {
                "family": "sessionize-submission",
                "scope": "synthetic bounded query",
                "observed_at": "2020-12-01T00:00:00Z",
                "evidence": wrapper,
            }
        ],
    }


def test_hydrated_positive_keeps_original_search_and_all_gaps(modules, tmp_path):
    source, owner = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    wrapper["search"]["next_page_token"] = "PRIVATE-CONTINUATION"
    wrapper["search"]["threads_omitted"] = 2
    wrapper["search"]["errors"] = [{"thread_id": "b002", "kind": "thread_unavailable"}]
    wrapper["search"]["threads"][0]["messages_omitted"] = 1
    wrapper["search"]["threads"][0]["message_count"] = 2
    before = deepcopy(packet)
    observations, scopes = source.prepare(packet)
    assert packet == before
    assert observations[0]["fact"]["status"] == "submitted"
    assert observations[0]["fact"]["complete"] is True
    assert observations[0]["fact"]["source_at"] == "2020-11-01T00:00:00+00:00"
    assert not scopes[0]["complete"]
    assert "search incomplete, paginated, omitted or failed" in scopes[0]["gaps"]
    assert "thread incomplete or messages omitted" in scopes[0]["gaps"]
    assert "message body incomplete" in scopes[0]["gaps"]
    path = tmp_path / "ledger"
    owner.record_many(path, observations, scopes)
    assert "PRIVATE-CONTINUATION" not in path.read_text()
    assert not owner.record_many(path, *source.prepare(packet))["changed"]


@pytest.mark.parametrize("partial,missing", [(True, False), (False, True)])
def test_failed_or_partial_hydration_never_becomes_positive(modules, partial, missing):
    source, _ = modules
    observations, scopes = source.prepare(hydrated_mail(partial=partial, missing=missing))
    assert observations[0]["fact"]["complete"] is False
    assert not scopes[0]["complete"]


def test_source2_complete_search_without_hydration_and_legacy_source1(modules, mail):
    source, _ = modules
    before = deepcopy(mail)
    old = source.prepare(mail)
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    full = wrapper["body_reads"][0]["evidence"]
    message = wrapper["search"]["threads"][0]["messages"][0]
    message.update(body=full["body"], body_complete=True)
    wrapper["search"]["complete"] = wrapper["search"]["threads"][0]["complete"] = True
    wrapper.update(body_reads=[], complete=True)
    observations, scopes = source.prepare(packet)
    assert observations[0]["fact"]["complete"] is True
    assert scopes[0]["gaps"] == [
        "proposal identity unverified; explicit owner/native binding required"
    ]
    assert source.prepare(mail) == old and mail == before


@pytest.mark.parametrize(
    "damage",
    [
        "outer_version",
        "source_kind",
        "wrapper_version",
        "wrapper_extra",
        "search_version",
        "search_extra",
        "read_extra",
        "id",
        "thread",
        "date",
        "duplicate_read",
        "complete_read",
        "future_fetch",
        "fetch_id",
        "fetch_thread",
        "citation",
        "trust",
        "body_bool",
        "body_bytes",
        "omitted",
        "missing_read",
        "missing_error",
        "extra_error",
        "duplicate_error",
        "unknown_error",
        "error_kind",
        "error_on_complete",
        "false_complete",
        "search_false_complete",
        "duplicate_message",
        "foreign_error",
        "wrapper_bytes",
        "search_bytes",
        "message_cap",
    ],
)
def test_source2_malformed_binding_or_coverage_holds_whole_import(modules, tmp_path, damage):
    source, owner = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    search = wrapper["search"]
    thread = search["threads"][0]
    read = wrapper["body_reads"][0]
    if damage == "outer_version":
        packet["schema_version"] = 3
    elif damage == "source_kind":
        packet["source"] = "sessionize"
    elif damage == "wrapper_version":
        wrapper["schema_version"] = True
    elif damage == "wrapper_extra":
        wrapper["guess"] = True
    elif damage == "search_version":
        search["schema_version"] = 2
    elif damage == "search_extra":
        search["guessed"] = True
    elif damage == "read_extra":
        read["role"] = "main"
    elif damage == "id":
        read["message_id"] = "a002"
    elif damage == "thread":
        read["thread_id"] = "b002"
    elif damage == "date":
        read["internalDate"] = "2020-11-02T00:00:00Z"
    elif damage == "duplicate_read":
        wrapper["body_reads"].append(deepcopy(read))
    elif damage == "complete_read":
        thread["messages"][0]["body_complete"] = True
    elif damage == "future_fetch":
        read["evidence"]["schema_version"] = 2
    elif damage == "fetch_id":
        read["evidence"]["message_id"] = "a002"
    elif damage == "fetch_thread":
        read["evidence"]["thread_id"] = "b002"
    elif damage == "citation":
        read["evidence"]["citation_url"] += "?secret=PRIVATE"
    elif damage == "trust":
        read["evidence"]["source_trust"] = "owner"
    elif damage == "body_bool":
        read["evidence"]["body_complete"] = 1
    elif damage == "body_bytes":
        read["evidence"]["body"] = "é" * (128 * 1024 + 1)
    elif damage == "omitted":
        wrapper["body_reads_omitted"] = True
    elif damage == "missing_read":
        wrapper["body_reads"] = []
    elif damage == "missing_error":
        wrapper["body_reads"] = []
        wrapper["body_reads_omitted"] = 1
    elif damage in {"extra_error", "error_on_complete"}:
        wrapper["errors"] = [{"message_id": "a001", "kind": "body_unavailable"}]
    elif damage == "duplicate_error":
        wrapper["body_reads"] = []
        wrapper["body_reads_omitted"] = 1
        wrapper["errors"] = [{"message_id": "a001", "kind": "body_unavailable"}] * 2
    elif damage == "unknown_error":
        wrapper["body_reads"] = []
        wrapper["body_reads_omitted"] = 1
        wrapper["errors"] = [{"message_id": "a001", "kind": "guessed"}]
    elif damage == "error_kind":
        read["evidence"]["body_complete"] = False
        wrapper["errors"] = [{"message_id": "a001", "kind": "binding_changed"}]
    elif damage == "false_complete":
        wrapper["complete"] = True
    elif damage == "search_false_complete":
        search["complete"] = True
    elif damage == "duplicate_message":
        thread["messages"].append(deepcopy(thread["messages"][0]))
    elif damage == "foreign_error":
        wrapper["errors"] = [{"message_id": "a002", "kind": "body_unavailable"}]
    elif damage == "wrapper_bytes":
        wrapper["errors"] = ["x" * (512 * 1024)]
    elif damage == "search_bytes":
        search["errors"] = [{"kind": "x" * (64 * 1024)}]
    else:
        thread["messages"] = [{**thread["messages"][0], "messageId": f"{i:x}"} for i in range(51)]
    # Valid earlier collection cannot commit when a later wrapper is malformed.
    packet["collections"].insert(0, deepcopy(hydrated_mail()["collections"][0]))
    path = tmp_path / "ledger"
    with pytest.raises(source.SourceError):
        owner.record_many(path, *source.prepare(packet))
    assert not path.exists()


@pytest.mark.parametrize(
    "kind", ["binding_changed", "body_unavailable", "body_too_large", "aggregate_limit"]
)
def test_source2_known_omission_categories_remain_unknown(modules, kind):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    packet["collections"][0]["evidence"]["errors"][0]["kind"] = kind
    observations, scopes = source.prepare(packet)
    assert not observations[0]["fact"]["complete"] and not scopes[0]["complete"]


def test_source2_exact_utf8_body_bound_retains_whole_body(modules):
    source, _ = modules
    packet = hydrated_mail()
    body = packet["collections"][0]["evidence"]["body_reads"][0]["evidence"]
    body["body"] = "éA" * ((256 * 1024) // 3) + "A"
    observations, scopes = source.prepare(packet)
    assert observations[0]["fact"]["complete"] is True
    assert observations[0]["fact"]["status"] is None
    assert not scopes[0]["complete"]


def test_source2_actual_import_and_late_bad_wrapper_preserve_ledger(
    modules, tmp_path, monkeypatch, capsys
):
    _, owner = modules
    path = tmp_path / "cfp-submissions.json"
    monkeypatch.setattr(owner, "LEDGER", path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(hydrated_mail())))
    assert owner.main(["import"]) == 1
    assert "source_transport" in capsys.readouterr().err
    assert not path.exists()
    # Public stdin cannot bypass the private transport; actual artifact import
    # and late-malformed multi-receipt atomicity are covered by its owner tests.
    source = modules[0]
    owner.record_many(path, *source.prepare(hydrated_mail()))
    before = path.read_bytes()
    packet = hydrated_mail()
    packet["collections"].append(deepcopy(packet["collections"][0]))
    packet["collections"][-1]["evidence"]["body_reads"][0]["internalDate"] = "2020-11-02"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(packet)))
    assert owner.main(["import"]) == 1
    captured = capsys.readouterr()
    assert "source_transport" in captured.err
    assert path.read_bytes() == before
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "damage",
    [
        "thread_missing",
        "thread_extra",
        "citation",
        "trust",
        "count",
        "count_bool",
        "omitted",
        "latest_id",
        "latest_date",
        "latest_direction",
        "header_missing",
        "header_extra",
        "header_large",
        "labels",
        "label_item",
        "error_missing_id",
        "error_extra",
        "error_kind",
        "complete_body_gap",
        "complete_thread_gap",
    ],
)
def test_search1_nested_contract_validates_before_any_commit(
    modules, tmp_path, source_version, damage
):
    source, owner = modules
    packet = hydrated_mail()
    search = packet["collections"][0]["evidence"]["search"]
    thread = search["threads"][0]
    message = thread["messages"][0]
    if damage == "thread_missing":
        del thread["citation_url"]
    elif damage == "thread_extra":
        thread["authority"] = True
    elif damage == "citation":
        thread["citation_url"] += "?PRIVATE-BEARER"
    elif damage == "trust":
        thread["source_trust"] = "trusted"
    elif damage == "count":
        thread["message_count"] = 2
    elif damage == "count_bool":
        thread["message_count"] = True
    elif damage == "omitted":
        thread["messages_omitted"] = -1
    elif damage == "latest_id":
        thread["thread_latest_message_id"] = "a002"
    elif damage == "latest_date":
        thread["thread_latest_message_date"] = "2020-10-01T00:00:00Z"
    elif damage == "latest_direction":
        thread["thread_latest_message_from"] = "me"
    elif damage == "header_missing":
        del message["from"]
    elif damage == "header_extra":
        message["threadId"] = "b001"
    elif damage == "header_large":
        message["subject"] = "X" * 301
    elif damage == "labels":
        message["labelIds"] = "SENT"
    elif damage == "label_item":
        message["labelIds"] = [1]
    elif damage == "error_missing_id":
        search["errors"] = [{"kind": "thread_unavailable"}]
    elif damage == "error_extra":
        search["errors"] = [{"kind": "invalid_thread_id", "raw": "PRIVATE"}]
    elif damage == "error_kind":
        search["errors"] = [{"kind": "future_error"}]
    elif damage == "complete_body_gap":
        thread["complete"] = True
    else:
        search["complete"] = True
    packet["schema_version"] = source_version
    if source_version == 1:
        packet["collections"][0]["evidence"] = search
    ledger_path = tmp_path / "ledger"
    with pytest.raises(source.SourceError):
        owner.record_many(ledger_path, *source.prepare(packet))
    assert not ledger_path.exists()


@pytest.mark.parametrize("suffix", ["Example Event 2020", "X" * 301])
def test_declined_subject_claims_are_bounded_without_losing_status(modules, tmp_path, suffix):
    source, owner = modules
    fields = source.mail_fields(
        "sessionize-sessions",
        "Your sessions at " + suffix,
        "The following sessions have not been accepted for Example Event",
    )
    assert fields["status"] == "declined"
    assert fields["event_name"] == ("Example Event 2020" if len(suffix) < 301 else None)
    fact = source.new_fact("mail", "bounded-decline", "2020-12-01T00:00:00Z", **fields)
    fact["citation"] = {"kind": "mail", "id": "a001", "url": None}
    assert owner.record_many(tmp_path / "ledger", [{"identity": None, "fact": fact}], [])["changed"]
    # Compose a canonical producer subject through search1 and the real ledger;
    # oversize direct-parser inputs still cannot pass the adapter.
    packet = hydrated_mail()
    search = packet["collections"][0]["evidence"]["search"]
    message = search["threads"][0]["messages"][0]
    message.update(
        subject="Your sessions at " + suffix,
        body="The following sessions have not been accepted for Example Event",
        body_complete=True,
    )
    search["complete"] = search["threads"][0]["complete"] = True
    packet["schema_version"] = 1
    packet["collections"][0]["evidence"] = search
    target = tmp_path / "composed-ledger"
    if len(message["subject"]) > 300:
        with pytest.raises(source.SourceError):
            owner.record_many(target, *source.prepare(packet))
        assert not target.exists()
    else:
        observations, scopes = source.prepare(packet)
        assert observations[0]["fact"]["event_name"] == "Example Event 2020"
        assert owner.record_many(target, observations, scopes)["changed"]


@pytest.mark.parametrize("metadata", ["omitted_latest", "tied_latest", "undated"])
def test_search1_valid_retained_latest_metadata_and_unknowns(modules, metadata):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    wrapper = packet["collections"][0]["evidence"]
    search = wrapper["search"]
    thread = search["threads"][0]
    if metadata == "omitted_latest":
        thread.update(
            message_count=2,
            messages_omitted=1,
            thread_latest_message_id="a002",
            thread_latest_message_date="2020-11-02T00:00:00+00:00",
        )
    elif metadata == "tied_latest":
        thread["messages"].append(
            {**thread["messages"][0], "messageId": "a002", "labelIds": ["SENT"]}
        )
        thread.update(
            message_count=2, thread_latest_message_id=None, thread_latest_message_from="unknown"
        )
        wrapper["body_reads_omitted"] = 2
        wrapper["errors"].append({"message_id": "a002", "kind": "body_unavailable"})
    else:
        thread["messages"][0]["internalDate"] = None
        thread.update(
            thread_latest_message_id=None,
            thread_latest_message_date=None,
            thread_latest_message_from="unknown",
        )
        wrapper["errors"][0]["kind"] = "binding_changed"
    observations, scopes = source.prepare(packet)
    assert observations and not any(item["complete"] for item in scopes)


@pytest.mark.parametrize("excess", [0, 1])
def test_sessionize_enforces_native_dom_utf8_aggregate_bound(modules, dashboard, excess):
    source, _ = modules
    capture = dashboard["capture"]
    # The Host bounds the compact native DOM before adding its scope metadata.

    def native_bytes():
        return len(
            json.dumps(
                {key: value for key, value in capture.items() if key != "scope"},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )

    target = 1_500_000 + excess
    while target - native_bytes() > 17_000:
        capture["nodes"].append({"tag": "DIV", "parent": 0, "classes": "", "text": "é" * 8000})
    padding = {"tag": "DIV", "parent": 0, "classes": "", "text": ""}
    capture["nodes"].append(padding)
    padding["text"] = "X" * (target - native_bytes())
    assert native_bytes() == target
    if excess:
        with pytest.raises(source.SourceError, match="capture_bounds"):
            source.prepare(dashboard)
    else:
        observations, scopes = source.prepare(dashboard)
        assert len(observations) == 51 and scopes[0]["complete"]


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("damage", ["unexplained_incomplete", "omitted_unknown", "thread_rebound"])
def test_mail_rejects_impossible_cross_field_relationships(modules, source_version, damage):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    search = wrapper["search"]
    thread = search["threads"][0]
    if damage == "unexplained_incomplete":
        thread["messages"][0]["body_complete"] = True
        wrapper["body_reads"] = []
    elif damage == "omitted_unknown":
        thread.update(
            message_count=2,
            messages_omitted=1,
            thread_latest_message_id="a002",
            thread_latest_message_date="2020-11-02T00:00:00+00:00",
            thread_latest_message_from="unknown",
        )
    else:
        duplicate = deepcopy(packet["collections"][0])
        moved = duplicate["evidence"]
        moved["search"]["threads"][0].update(
            thread_id="b002", citation_url="https://mail.google.com/mail/u/0/#all/b002"
        )
        moved["body_reads"][0]["thread_id"] = "b002"
        moved["body_reads"][0]["evidence"].update(
            thread_id="b002", citation_url="https://mail.google.com/mail/u/0/#all/b002"
        )
        packet["collections"].append(duplicate)
    packet["schema_version"] = source_version
    if source_version == 1:
        for collection in packet["collections"]:
            collection["evidence"] = collection["evidence"]["search"]
    with pytest.raises(source.SourceError):
        source.prepare(packet)


@pytest.mark.parametrize("source_version", [1, 2])
def test_mail_preserves_valid_continuation_without_inventing_a_thread_gap(modules, source_version):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    thread = wrapper["search"]["threads"][0]
    thread["messages"][0]["body_complete"] = thread["complete"] = True
    wrapper["body_reads"] = []
    # A noninitial query page can have no local gaps and still be globally incomplete.
    assert not wrapper["search"]["complete"]
    packet["schema_version"] = source_version
    if source_version == 1:
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, scopes = source.prepare(packet)
    assert observations and not any(item["complete"] for item in scopes)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "relationship", ["omitted_retained", "retained_omitted", "omitted_omitted"]
)
def test_latest_omitted_mail_ids_keep_global_native_thread_binding(
    modules, source_version, relationship
):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    first = packet["collections"][0]
    second = deepcopy(first)
    packet["collections"].append(second)
    for collection, tid, mid in ((first, "b001", "a001"), (second, "b002", "a002")):
        wrapper = collection["evidence"]
        thread = wrapper["search"]["threads"][0]
        thread["messages"][0]["messageId"] = mid
        wrapper["errors"][0]["message_id"] = mid
        thread.update(
            thread_id=tid,
            citation_url=f"https://mail.google.com/mail/u/0/#all/{tid}",
            thread_latest_message_id=mid,
        )
    if relationship == "omitted_retained":
        omitted, repeated = first, "a002"
    elif relationship == "retained_omitted":
        omitted, repeated = second, "a001"
    else:
        omitted, repeated = first, "a003"
        second["evidence"]["search"]["threads"][0].update(
            message_count=2,
            messages_omitted=1,
            thread_latest_message_id=repeated,
            thread_latest_message_date="2020-11-02T00:00:00+00:00",
        )
    omitted["evidence"]["search"]["threads"][0].update(
        message_count=2,
        messages_omitted=1,
        thread_latest_message_id=repeated,
        thread_latest_message_date="2020-11-02T00:00:00+00:00",
    )
    packet["schema_version"] = source_version
    if source_version == 1:
        for collection in packet["collections"]:
            collection["evidence"] = collection["evidence"]["search"]
    with pytest.raises(source.SourceError, match="mail_identity_conflict"):
        source.prepare(packet)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("retained", [0, 1])
@pytest.mark.parametrize("excess", [0, 1])
def test_search_retained_and_omitted_threads_share_the_native_page_budget(
    modules, source_version, retained, excess
):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    wrapper = packet["collections"][0]["evidence"]
    search = wrapper["search"]
    search["threads_omitted"] = 30 - retained + excess
    if not retained:
        search["threads"] = []
        wrapper["body_reads_omitted"] = 0
        wrapper["errors"] = []
    packet["schema_version"] = source_version
    if source_version == 1:
        packet["collections"][0]["evidence"] = search
    if excess:
        with pytest.raises(source.SourceError, match="mail_threads"):
            source.prepare(packet)
    else:
        observations, scopes = source.prepare(packet)
        assert len(observations) == retained and not scopes[0]["complete"]


@pytest.mark.parametrize("surface", ["events", "sessions"])
def test_oversized_rendered_count_has_actionable_atomic_cli_refusal(
    modules, dashboard, tmp_path, monkeypatch, capsys, surface
):
    source, owner = modules
    nodes = dashboard["capture"]["nodes"]
    if surface == "events":
        target = next(node for node in nodes if node["tag"] == "SMALL" and node["text"] == "(16)")
        target["text"] = "(" + "9" * 5000 + ")"
    else:
        target = next(node for node in nodes if node["text"].endswith(" submitted"))
        target["text"] = "9" * 5000 + " submitted"
    with pytest.raises(source.SourceError, match="capture_count"):
        source.prepare(dashboard)
    ledger_path = tmp_path / "ledger"
    monkeypatch.setattr(owner, "LEDGER", ledger_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(dashboard)))
    assert owner.main(["import"]) == 1
    output = capsys.readouterr()
    assert not output.out and "capture_count" in output.err and "Traceback" not in output.err
    assert not ledger_path.exists()


def test_sessionize_refuses_a_non_body_native_root(modules, dashboard):
    source, _ = modules
    dashboard["capture"]["nodes"][0]["tag"] = "DIV"
    with pytest.raises(source.SourceError, match="capture_tree"):
        source.prepare(dashboard)


@pytest.mark.parametrize(
    "scope", ["arbitrary collection", "future version", "Complete rendered node tree"]
)
def test_sessionize_requires_the_exact_published_capture_scope(modules, dashboard, scope):
    source, _ = modules
    dashboard["capture"]["scope"] = scope
    with pytest.raises(source.SourceError, match="capture_provenance"):
        source.prepare(dashboard)


def failed_stub_packet(source_version, retained, omitted, errors):
    capture = deepcopy(mail_packet()["collections"][-1])
    template = capture["evidence"]["threads"][1]
    threads = []
    for index in range(retained):
        thread = deepcopy(template)
        tid, mid = f"b{index:03x}", f"c{index:03x}"
        thread.update(
            thread_id=tid,
            citation_url=f"https://mail.google.com/mail/u/0/#all/{tid}",
            thread_latest_message_id=mid,
        )
        thread["messages"][0]["messageId"] = mid
        threads.append(thread)
    search = capture["evidence"]
    search.update(threads=threads, threads_omitted=omitted, errors=errors, complete=False)
    malformed = {error.get("thread_id") for error in errors if error["kind"] == "malformed_payload"}
    incomplete = []
    for thread in threads:
        if thread["thread_id"] in malformed:
            thread["complete"] = False
            message = thread["messages"][0]
            message.update(body="", body_complete=False, subject="", **{"from": "", "to": ""})
            incomplete.append({"message_id": message["messageId"], "kind": "body_unavailable"})
    if source_version == 2:
        capture["evidence"] = {
            "schema_version": 1,
            "search": search,
            "body_reads": [],
            "body_reads_omitted": len(incomplete),
            "complete": False,
            "errors": incomplete,
        }
    return {"schema_version": source_version, "source": "mail", "collections": [capture]}


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "kind",
    [
        "invalid_thread_id",
        "thread_unavailable",
        "thread_shape",
        "thread_identity",
        "malformed_thread",
    ],
)
@pytest.mark.parametrize("excess", [0, 1])
def test_failed_stubs_share_the_native_search_page_budget(modules, source_version, kind, excess):
    source, _ = modules
    error = {"kind": kind}
    if kind != "invalid_thread_id":
        error["thread_id"] = "fff1"
    packet = failed_stub_packet(source_version, 29 + excess, 0, [error])
    if excess:
        with pytest.raises(source.SourceError, match="mail_threads"):
            source.prepare(packet)
    else:
        observations, scopes = source.prepare(packet)
        assert len(observations) == 29 and not scopes[0]["complete"]


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("retained", [28, 29])
def test_failed_malformed_stub_counts_once_alongside_byte_omissions(
    modules, source_version, retained
):
    source, _ = modules
    errors = [
        {"kind": "malformed_payload", "thread_id": "fff1"},
        {"kind": "malformed_thread", "thread_id": "fff1"},
    ]
    packet = failed_stub_packet(source_version, retained, 1, errors)
    if retained == 29:
        with pytest.raises(source.SourceError, match="mail_threads"):
            source.prepare(packet)
    else:
        assert len(source.prepare(packet)[0]) == 28


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("omitted", [0, 1])
def test_byte_omitted_malformed_record_uses_its_existing_omission_slot(
    modules, source_version, omitted
):
    source, _ = modules
    packet = failed_stub_packet(
        source_version, 29, omitted, [{"kind": "malformed_payload", "thread_id": "fff1"}]
    )
    if omitted:
        assert len(source.prepare(packet)[0]) == 29
    else:
        with pytest.raises(source.SourceError, match="mail_search_coverage"):
            source.prepare(packet)


@pytest.mark.parametrize("reverse", [False, True])
def test_duplicate_collection_advances_existing_fact_to_latest_sighting(
    modules, mail, tmp_path, reverse
):
    source, owner = modules
    capture = deepcopy(mail["collections"][-1])
    capture["evidence"]["threads"] = [capture["evidence"]["threads"][1]]
    capture["evidence"].update(complete=True, next_page_token=None, threads_omitted=0, errors=[])
    capture["observed_at"] = "2020-12-01T10:30:00Z"
    path = tmp_path / "ledger"
    initial = source.prepare({"schema_version": 1, "source": "mail", "collections": [capture]})
    owner.record_many(path, *initial)
    before = owner.read(path)
    early, late = deepcopy(capture), deepcopy(capture)
    early["family"], early["scope"] = "cfpdev", "independent broader query"
    early["observed_at"], late["observed_at"] = "2020-12-01T10:00:00Z", "2020-12-01T11:00:00Z"
    collections = [late, early] if reverse else [early, late]
    observations, scopes = source.prepare(
        {"schema_version": 1, "source": "mail", "collections": collections}
    )
    assert len(observations) == 1
    assert observations[0]["fact"]["observed_at"] == late["observed_at"]
    owner.record_many(path, observations, scopes)
    after = owner.read(path)
    assert set(after["unresolved"]) == set(before["unresolved"])
    old = next(iter(before["unresolved"].values()))
    new = next(iter(after["unresolved"].values()))
    assert new["evidence_id"] == old["evidence_id"]
    assert new["source_at"] == old["source_at"]
    assert new["observed_at"] == late["observed_at"]


@pytest.mark.parametrize("source_version", [1, 2])
def test_retained_malformed_payload_does_not_consume_an_extra_stub(modules, source_version):
    source, _ = modules
    packet = failed_stub_packet(
        source_version, 30, 0, [{"kind": "malformed_payload", "thread_id": "b000"}]
    )
    search = packet["collections"][0]["evidence"]
    if source_version == 2:
        search = search["search"]
    search["threads"][0]["complete"] = False
    assert len(source.prepare(packet)[0]) == 30


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "kinds",
    [
        ["thread_unavailable", "thread_unavailable"],
        ["malformed_payload", "thread_unavailable"],
        ["thread_shape", "thread_identity"],
        ["malformed_thread", "malformed_payload"],
    ],
)
def test_one_lookup_cannot_have_duplicate_or_incompatible_failure_stages(
    modules, source_version, kinds
):
    source, _ = modules
    packet = failed_stub_packet(
        source_version, 1, 0, [{"kind": kind, "thread_id": "fff1"} for kind in kinds]
    )
    with pytest.raises(source.SourceError, match="mail_search_error"):
        source.prepare(packet)


@pytest.mark.parametrize("reverse", [False, True])
def test_equal_instant_sightings_use_a_deterministic_literal_tie_break(modules, mail, reverse):
    source, _ = modules
    capture = deepcopy(mail["collections"][-1])
    capture["evidence"]["threads"] = [capture["evidence"]["threads"][1]]
    capture["evidence"].update(complete=True, next_page_token=None, threads_omitted=0, errors=[])
    other = deepcopy(capture)
    capture["observed_at"], other["observed_at"] = (
        "2020-12-01T11:00:00Z",
        "2020-12-01T13:00:00+02:00",
    )
    collections = [other, capture] if reverse else [capture, other]
    observations, _ = source.prepare(
        {"schema_version": 1, "source": "mail", "collections": collections}
    )
    assert observations[0]["fact"]["observed_at"] == max(
        capture["observed_at"], other["observed_at"]
    )


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "kinds,token",
    [
        (["page_limit_exceeded"] * 4, None),
        (["page_token_too_large"] * 2, None),
        (["repeated_page_token"] * 2, "cursor"),
        (["page_token_too_large", "repeated_page_token"], "cursor"),
        (["page_token_too_large"], "cursor"),
        (["repeated_page_token"], None),
        (["repeated_page_token", "page_limit_exceeded"], "cursor"),
        (["invalid_thread_id", "page_limit_exceeded"], None),
    ],
)
def test_search_refuses_impossible_page_error_relationships(modules, source_version, kinds, token):
    source, _ = modules
    packet = failed_stub_packet(source_version, 1, 0, [{"kind": kind} for kind in kinds])
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    search["next_page_token"] = token
    with pytest.raises(source.SourceError, match="mail_search_error"):
        source.prepare(packet)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "kinds,token",
    [
        (["page_limit_exceeded"] * 3, None),
        (["page_limit_exceeded"] * 3, "cursor"),
        (["page_limit_exceeded"] * 3 + ["page_token_too_large"], None),
        (["page_limit_exceeded"] * 3 + ["repeated_page_token"], "cursor"),
    ],
)
def test_native_terminal_page_errors_remain_supported(modules, source_version, kinds, token):
    source, _ = modules
    packet = failed_stub_packet(source_version, 1, 0, [{"kind": kind} for kind in kinds])
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    search["next_page_token"] = token
    observations, scopes = source.prepare(packet)
    assert len(observations) == 1 and not scopes[0]["complete"]


@pytest.mark.parametrize("partial", [False, True])
def test_hydrated_subject_mismatch_cannot_combine_observations_or_commit(
    modules, tmp_path, partial
):
    source, owner = modules
    packet = hydrated_mail(partial=partial)
    packet["collections"][0]["evidence"]["body_reads"][0]["evidence"]["subject"] = "Unrelated mail"
    path = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="mail_body_binding"):
        observations, scopes = source.prepare(packet)
        owner.record_many(path, observations, scopes)
    assert not path.exists()


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("field", ["subject", "from", "to"])
def test_native_sanitized_header_truncation_shape_is_supported(modules, source_version, field):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    message = wrapper["search"]["threads"][0]["messages"][0]
    message[field] = "X" * 300 + " [...]"
    if field != "to":
        wrapper["body_reads"][0]["evidence"][field] = message[field]
    if source_version == 1:
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, scopes = source.prepare(packet)
    assert len(observations) == 1 and not scopes[0]["complete"]
    if field == "subject":
        assert observations[0]["fact"]["status"] is None


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("interleaved", [False, True])
@pytest.mark.parametrize("other", ["invalid_thread_id", "thread_unavailable"])
def test_native_two_stage_failure_pair_is_consecutive(modules, source_version, interleaved, other):
    source, _ = modules
    payload = {"kind": "malformed_payload", "thread_id": "fff1"}
    parser = {"kind": "malformed_thread", "thread_id": "fff1"}
    between = {"kind": other}
    if other == "thread_unavailable":
        between["thread_id"] = "fff2"
    errors = [payload, between, parser] if interleaved else [payload, parser, between]
    packet = failed_stub_packet(source_version, 1, 0, errors)
    if interleaved:
        with pytest.raises(source.SourceError, match="mail_search_error"):
            source.prepare(packet)
    else:
        observations, scopes = source.prepare(packet)
        assert len(observations) == 1 and not scopes[0]["complete"]


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("damage", ["earlier_omitted", "retained_reversed"])
def test_malformed_search_errors_preserve_retained_prefix(modules, source_version, damage):
    source, _ = modules
    packet = failed_stub_packet(
        source_version,
        2,
        0,
        [{"kind": "malformed_payload", "thread_id": tid} for tid in ("b000", "b001")],
    )
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    for thread in search["threads"]:
        thread["complete"] = False
    if damage == "earlier_omitted":
        search["threads"].pop(0)
        search["threads_omitted"] = 1
    else:
        search["errors"].reverse()
    with pytest.raises(source.SourceError, match="mail_search_coverage"):
        source.prepare(packet)


@pytest.mark.parametrize("source_version", [1, 2])
def test_malformed_search_suffix_omission_remains_supported(modules, source_version):
    source, _ = modules
    packet = failed_stub_packet(
        source_version,
        2,
        0,
        [{"kind": "malformed_payload", "thread_id": tid} for tid in ("b000", "b001")],
    )
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    search["threads"][0]["complete"] = False
    search["threads"].pop()
    search["threads_omitted"] = 1
    if source_version == 2:
        evidence["errors"] = evidence["errors"][:1]
        evidence["body_reads_omitted"] = 1
    observations, scopes = source.prepare(packet)
    assert len(observations) == 1 and not scopes[0]["complete"]


def ordered_hydrated_mail():
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    first_thread, first_read = wrapper["search"]["threads"][0], wrapper["body_reads"][0]
    for tid, mid in (("b002", "a002"), ("b003", "a003")):
        thread, read = deepcopy(first_thread), deepcopy(first_read)
        citation = f"https://mail.google.com/mail/u/0/#all/{tid}"
        thread.update(thread_id=tid, citation_url=citation, thread_latest_message_id=mid)
        thread["messages"][0]["messageId"] = mid
        read.update(message_id=mid, thread_id=tid)
        read["evidence"].update(message_id=mid, thread_id=tid, citation_url=citation)
        wrapper["search"]["threads"].append(thread)
        wrapper["body_reads"].append(read)
    return packet


@pytest.mark.parametrize("damage", ["earlier_aggregate", "read_order", "error_order"])
def test_hydration_preserves_candidate_order_and_aggregate_suffix(modules, damage):
    source, _ = modules
    packet = ordered_hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    if damage == "earlier_aggregate":
        wrapper["body_reads"].pop(0)
        wrapper["errors"] = [{"message_id": "a001", "kind": "aggregate_limit"}]
        wrapper["body_reads_omitted"] = 1
    elif damage == "read_order":
        wrapper["body_reads"].reverse()
    else:
        wrapper["body_reads"] = wrapper["body_reads"][:1]
        wrapper["errors"] = [
            {"message_id": mid, "kind": "body_unavailable"} for mid in ("a003", "a002")
        ]
        wrapper["body_reads_omitted"] = 2
    with pytest.raises(source.SourceError, match="mail_handoff_coverage"):
        source.prepare(packet)


@pytest.mark.parametrize("failure_first", [False, True])
def test_hydration_suffix_omission_preserves_independent_failures(modules, failure_first):
    source, _ = modules
    packet = ordered_hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    failed, retained = ("a001", "a002") if failure_first else ("a002", "a001")
    wrapper["body_reads"] = [
        read for read in wrapper["body_reads"] if read["message_id"] == retained
    ]
    wrapper["errors"] = [
        {"message_id": failed, "kind": "body_unavailable"},
        {"message_id": "a003", "kind": "aggregate_limit"},
    ]
    wrapper["body_reads_omitted"] = 2
    observations, scopes = source.prepare(packet)
    assert len(observations) == 3 and not scopes[0]["complete"]
    assert sum(item["fact"]["complete"] for item in observations) == 1


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("field", ["subject", "from", "to"])
@pytest.mark.parametrize(
    "header",
    [
        "Example\tEvent",
        "Example\r\nEvent",
        "Example  Event",
        " Example Event",
        "Example Event ",
        "Example\u200bEvent",
        "Ｅvent",
        "Cafe\u0301",
        "Ϗ Ϗ Ϗ Ϗ",
        "Example\x7fEvent",
        "Example\u034fEvent",
        "Example\u115fEvent",
    ],
)
def test_mail_headers_must_already_match_sanitized_producer_form(
    modules, tmp_path, source_version, field, header
):
    source, owner = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    wrapper["search"]["threads"][0]["messages"][0][field] = header
    if field != "to":
        wrapper["body_reads"][0]["evidence"][field] = header
    if source_version == 1:
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="mail_header"):
        owner.record_many(ledger, *source.prepare(packet))
    assert not ledger.exists()


@pytest.mark.parametrize("header", ["Example\tSender", "Example  Sender", "Ｓender"])
def test_hydrated_sender_requires_canonical_producer_form(modules, header):
    source, _ = modules
    packet = hydrated_mail()
    packet["collections"][0]["evidence"]["body_reads"][0]["evidence"]["from"] = header
    with pytest.raises(source.SourceError, match="mail_header"):
        source.prepare(packet)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "header", ["café", "Привет", "עברית", "अंतर", "ASCII!!!!", "X" * 299 + "  [...]"]
)
def test_canonical_unicode_and_truncation_boundary_headers_remain_supported(
    modules, source_version, header
):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    wrapper["search"]["threads"][0]["messages"][0]["from"] = header
    wrapper["body_reads"][0]["evidence"]["from"] = header
    if source_version == 1:
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, scopes = source.prepare(packet)
    assert len(observations) == 1 and not scopes[0]["complete"]


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "insert", ["<b>received</b>", "https://example.invalid/PRIVATE?token=SECRET received"]
)
def test_literal_native_body_content_cannot_be_rewritten_into_a_submission(
    modules, source_version, insert
):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    body = wrapper["body_reads"][0]["evidence"]["body"].replace("have received", "have " + insert)
    wrapper["body_reads"][0]["evidence"]["body"] = body
    if source_version == 1:
        message = wrapper["search"]["threads"][0]["messages"][0]
        message.update(body=body, body_complete=True)
        wrapper["search"]["threads"][0]["complete"] = True
        wrapper["search"]["complete"] = True
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, _ = source.prepare(packet)
    assert observations[0]["fact"]["status"] is None
    assert "PRIVATE" not in json.dumps(observations) and "SECRET" not in json.dumps(observations)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "body",
    [
        "We\thave received",
        "We  have received",
        "Ｗe have received",
        "We\u200bhave received",
        "We have received\r\n",
    ],
)
def test_native_body_must_already_have_sanitized_form_before_any_commit(
    modules, tmp_path, source_version, body
):
    source, owner = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    if source_version == 1:
        wrapper["search"]["threads"][0]["messages"][0]["body"] = body
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    else:
        wrapper["body_reads"][0]["evidence"]["body"] = body
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="mail_body"):
        owner.record_many(ledger, *source.prepare(packet))
    assert not ledger.exists()


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("field", ["message", "latest"])
@pytest.mark.parametrize(
    "stamp",
    [
        "2020-11-01T00:00:00Z",
        "2020-11-01T00:00:00.000+00:00",
        "2020-11-01T00:00:00.123+00:00",
        "2020-11-01 00:00:00+00:00",
        "20201101T000000+00:00",
        "2020-11-01T00:00:00.123456+00:00",
        "2020-11-01T00:00:00+0000",
    ],
)
def test_native_mail_dates_require_exact_millisecond_derived_serialization(
    modules, tmp_path, source_version, field, stamp
):
    from datetime import datetime

    source, owner = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    thread = wrapper["search"]["threads"][0]
    if field == "message":
        thread["messages"][0]["internalDate"] = stamp
        thread["thread_latest_message_date"] = datetime.fromisoformat(stamp).isoformat()
        wrapper["body_reads"][0]["internalDate"] = stamp
    else:
        thread.update(
            thread_latest_message_date=stamp,
            thread_latest_message_id="a099",
            messages_omitted=1,
            message_count=2,
        )
    if source_version == 1:
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="mail_.*date|source_time"):
        owner.record_many(ledger, *source.prepare(packet))
    assert not ledger.exists()


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("stamp", ["2020-11-01T00:00:00+00:00", "2020-11-01T00:00:00.123000+00:00"])
def test_canonical_native_date_precision_remains_unchanged(modules, source_version, stamp):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    thread = wrapper["search"]["threads"][0]
    thread["messages"][0]["internalDate"] = stamp
    thread["thread_latest_message_date"] = stamp
    wrapper["body_reads"][0]["internalDate"] = stamp
    if source_version == 1:
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, _ = source.prepare(packet)
    assert observations[0]["fact"]["source_at"] == stamp


@pytest.mark.parametrize("source_version", [1, 2])
def test_literal_bearer_url_inside_a_template_claim_remains_unknown(modules, source_version):
    source, _ = modules
    packet = hydrated_mail()
    wrapper = packet["collections"][0]["evidence"]
    title = "https://example.invalid/PRIVATE?token=SECRET"
    subject = "Your proposal: " + title
    body = f'We have received your proposal "{title}" to Example Event. We will notify you'
    message = wrapper["search"]["threads"][0]["messages"][0]
    message["subject"] = subject
    wrapper["body_reads"][0]["evidence"].update(subject=subject, body=body)
    if source_version == 1:
        message.update(body=body, body_complete=True)
        wrapper["search"]["threads"][0]["complete"] = True
        wrapper["search"]["complete"] = True
        packet["schema_version"] = 1
        packet["collections"][0]["evidence"] = wrapper["search"]
    observations, _ = source.prepare(packet)
    assert observations[0]["fact"]["status"] is None
    assert "PRIVATE" not in json.dumps(observations) and "SECRET" not in json.dumps(observations)


@pytest.mark.parametrize(
    "stamp",
    [
        "2020-12-01T00:00:00Z",
        "2020-12-01T00:00:00.000000Z",
        "2020-12-01T00:00:00.000+00:00",
        "2020-12-01T00:00:00.000+01:00",
        "2020-12-01T00:00:00.1Z",
        "2020-12-01T00:00:00.123456Z",
        "2020-12-01T00:00:00.1234Z",
        "20201201T000000Z",
    ],
)
def test_sessionize_requires_native_javascript_capture_clock(modules, dashboard, tmp_path, stamp):
    source, owner = modules
    dashboard["observed_at"] = stamp
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="capture_time"):
        owner.record_many(ledger, *source.prepare(dashboard))
    assert not ledger.exists()


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize("damage", ["complete_template", "incomplete_template", "retained_headers"])
def test_malformed_payload_needs_an_actual_retained_or_omitted_malformed_message(
    modules, tmp_path, source_version, damage
):
    source, owner = modules
    packet = failed_stub_packet(
        source_version, 1, 0, [{"kind": "malformed_payload", "thread_id": "b000"}]
    )
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    message = search["threads"][0]["messages"][0]
    message["subject"] = "Your sessions at Example Event 2020"
    if damage != "retained_headers":
        message["body"] = "The following sessions have not been accepted for Example Event"
        message["body_complete"] = damage == "complete_template"
    if source_version == 2 and message["body_complete"]:
        evidence.update(body_reads_omitted=0, errors=[])
    ledger = tmp_path / "ledger"
    with pytest.raises(source.SourceError, match="mail_thread_coverage"):
        owner.record_many(ledger, *source.prepare(packet))
    assert not ledger.exists()


@pytest.mark.parametrize("source_version", [1, 2])
def test_omitted_malformed_message_can_coexist_with_a_complete_retained_template(
    modules, source_version
):
    source, _ = modules
    packet = failed_stub_packet(
        source_version, 1, 0, [{"kind": "malformed_payload", "thread_id": "b000"}]
    )
    evidence = packet["collections"][0]["evidence"]
    search = evidence["search"] if source_version == 2 else evidence
    thread = search["threads"][0]
    thread.update(message_count=2, messages_omitted=1)
    thread["messages"][0].update(
        subject="Your sessions at Example Event 2020",
        body="The following sessions have not been accepted for Example Event",
        body_complete=True,
    )
    if source_version == 2:
        evidence.update(body_reads_omitted=0, errors=[])
    observations, scopes = source.prepare(packet)
    assert observations[0]["fact"]["status"] == "declined"
    assert not scopes[0]["complete"]


@pytest.mark.parametrize("source_version", [1, 2])
def test_native_mail_order_is_retained_without_chronological_reinterpretation(
    modules, source_version
):
    source, _ = modules
    # Exact output of the published Admin search owner with a synthetic native
    # newer-first response. parse_thread preserves order; latest_thread_state
    # independently chooses the newest date. No native sorting contract exists.
    evidence = {
        "schema_version": 1,
        "threads": [
            {
                "thread_id": "aa",
                "citation_url": "https://mail.google.com/mail/u/0/#all/aa",
                "source_trust": "untrusted",
                "messages": [
                    {
                        "messageId": "a1",
                        "from": "speaker@example.invalid",
                        "to": "owner@example.invalid",
                        "subject": "Synthetic producer order proof",
                        "internalDate": "2021-01-01T00:00:00+00:00",
                        "labelIds": ["INBOX"],
                        "body": "Synthetic bounded body",
                        "body_complete": True,
                    },
                    {
                        "messageId": "a2",
                        "from": "speaker@example.invalid",
                        "to": "owner@example.invalid",
                        "subject": "Synthetic producer order proof",
                        "internalDate": "2020-12-31T00:00:00+00:00",
                        "labelIds": ["INBOX"],
                        "body": "Synthetic bounded body",
                        "body_complete": True,
                    },
                ],
                "message_count": 2,
                "complete": True,
                "thread_latest_message_from": "them",
                "thread_latest_message_id": "a1",
                "thread_latest_message_date": "2021-01-01T00:00:00+00:00",
                "messages_omitted": 0,
            }
        ],
        "next_page_token": None,
        "threads_omitted": 0,
        "complete": True,
        "errors": [],
    }
    if source_version == 2:
        evidence = {
            "schema_version": 1,
            "search": evidence,
            "body_reads": [],
            "body_reads_omitted": 0,
            "complete": True,
            "errors": [],
        }
    packet = {
        "schema_version": source_version,
        "source": "mail",
        "collections": [
            {
                "family": "sessionize-submission",
                "scope": "synthetic",
                "observed_at": "2021-01-02T00:00:00Z",
                "evidence": evidence,
            }
        ],
    }
    observations, scopes = source.prepare(packet)
    assert len(observations) == 2 and len(scopes) == 1
    assert {row["fact"]["source_at"] for row in observations} == {
        "2021-01-01T00:00:00+00:00",
        "2020-12-31T00:00:00+00:00",
    }
    assert all(row["fact"]["status"] is None for row in observations)


@pytest.mark.parametrize("source_version", [1, 2])
@pytest.mark.parametrize(
    "case,held",
    [
        ("unique_unknown", True),
        ("unique_null_id", True),
        ("outside_id_retained_tie", True),
        ("id_with_retained_tie", True),
        ("wrong_tie_direction", True),
        ("unknown_tie", False),
        ("two_omitted_tie", False),
        ("same_direction_tie", False),
        ("unique_omitted", False),
    ],
)
def test_omitted_latest_metadata_has_a_possible_native_witness(modules, source_version, case, held):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    wrapper = packet["collections"][0]["evidence"]
    thread = wrapper["search"]["threads"][0]
    thread.update(
        message_count=2,
        messages_omitted=1,
        thread_latest_message_date="2020-11-02T00:00:00+00:00",
        thread_latest_message_id=None,
        thread_latest_message_from="unknown",
    )
    if case == "unique_null_id":
        thread["thread_latest_message_from"] = "them"
    elif case in {
        "outside_id_retained_tie",
        "id_with_retained_tie",
        "wrong_tie_direction",
        "unknown_tie",
        "same_direction_tie",
    }:
        thread["thread_latest_message_date"] = thread["messages"][0]["internalDate"]
        if case == "outside_id_retained_tie":
            thread.update(thread_latest_message_id="a002", thread_latest_message_from="them")
        elif case == "id_with_retained_tie":
            other = deepcopy(thread["messages"][0])
            other.update(messageId="a003", body="Synthetic complete body", body_complete=True)
            thread["messages"].append(other)
            thread.update(
                message_count=3, thread_latest_message_id="a001", thread_latest_message_from="them"
            )
        elif case == "wrong_tie_direction":
            thread["messages"][0]["labelIds"] = ["SENT"]
            thread["thread_latest_message_from"] = "them"
        elif case == "same_direction_tie":
            thread["thread_latest_message_from"] = "them"
    elif case == "two_omitted_tie":
        thread.update(message_count=3, messages_omitted=2)
    elif case == "unique_omitted":
        thread.update(thread_latest_message_id="a002", thread_latest_message_from="them")
    packet["schema_version"] = source_version
    if source_version == 1:
        packet["collections"][0]["evidence"] = wrapper["search"]
    if held:
        with pytest.raises(source.SourceError, match="mail_latest_metadata"):
            source.prepare(packet)
    else:
        observations, scopes = source.prepare(packet)
        assert len(observations) == len(thread["messages"]) and not scopes[0]["complete"]


@pytest.mark.parametrize(
    "kind", ["body_unavailable", "body_too_large", "aggregate_limit", "binding_changed"]
)
def test_undated_hydration_omission_requires_the_native_binding_error(modules, kind):
    source, _ = modules
    packet = hydrated_mail(missing=True)
    wrapper = packet["collections"][0]["evidence"]
    thread = wrapper["search"]["threads"][0]
    thread["messages"][0]["internalDate"] = None
    thread.update(
        thread_latest_message_id=None,
        thread_latest_message_date=None,
        thread_latest_message_from="unknown",
    )
    wrapper["errors"][0]["kind"] = kind
    if kind != "binding_changed":
        with pytest.raises(source.SourceError, match="mail_handoff_coverage"):
            source.prepare(packet)
    else:
        observations, scopes = source.prepare(packet)
        assert len(observations) == 1 and not scopes[0]["complete"]


@pytest.mark.parametrize("separator", ["\u00a0", "\u2003", "\u0085", "\v"])
@pytest.mark.parametrize("surface", ["account", "filter", "event", "status", "pagination"])
def test_raw_class_tokens_use_only_html_ascii_whitespace(modules, dashboard, separator, surface):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    if surface == "pagination":
        nodes.append(
            {
                "tag": "DIV",
                "parent": 0,
                "classes": "pagination" + separator + "extra",
                "text": "More",
            }
        )
    elif surface == "account":
        target = next(n for n in nodes if "event-title" in n["classes"].split())
        target["classes"] = "event-title" + separator + "extra"
    elif surface == "filter":
        target = next(
            n
            for i, n in enumerate(nodes)
            if n["tag"] == "LI"
            and "active" in n["classes"].split()
            and any(child["parent"] == i and child["text"] == "Current events" for child in nodes)
        )
        target["classes"] = "active" + separator + "extra"
    elif surface == "event":
        target = next(n for n in nodes if n["tag"] == "TR" and "event" in n["classes"].split())
        target["classes"] = "event" + separator + "gray-bg"
    else:
        target = next(n for n in nodes if "session-status_0" in n["classes"].split())
        target["classes"] = "badge" + separator + "session-status_0"
    if surface in {"account", "filter", "event"}:
        with pytest.raises(source.SourceError):
            source.prepare(dashboard)
    else:
        observations, scopes = source.prepare(dashboard)
        if surface == "status":
            assert observations[0]["fact"]["status"] is None and not scopes[0]["complete"]
        else:
            assert len(observations) == 51 and scopes[0]["complete"]


@pytest.mark.parametrize("surface", ["event", "proposal"])
@pytest.mark.parametrize("competing", [False, True])
def test_native_lowercase_svg_anchors_participate_in_identity_binding(
    modules, dashboard, surface, competing
):
    source, _ = modules
    nodes = dashboard["capture"]["nodes"]
    route = "/event/details/" if surface == "event" else "/session/"
    index = next(i for i, n in enumerate(nodes) if route in n.get("href", ""))
    if competing:
        duplicate = {**nodes[index], "tag": "a"}
        duplicate["href"] = (
            "https://sessionize.com/app/speaker/event/details/999999"
            if surface == "event"
            else "https://sessionize.com/app/speaker/session/competing"
        )
        insertion = index + 1
        for node in nodes[insertion:]:
            if node["parent"] >= insertion:
                node["parent"] += 1
        nodes.insert(insertion, duplicate)
        with pytest.raises(source.SourceError, match="capture_.*_identity"):
            source.prepare(dashboard)
    else:
        nodes[index]["tag"] = "a"
        observations, scopes = source.prepare(dashboard)
        assert len(observations) == 51 and scopes[0]["complete"]
