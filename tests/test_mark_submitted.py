"""Submission confirmations against mail shaped like the owner's real messages."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills/nightly-cfp-sync/scripts/mark-submitted.py"
TODAY = "2026-11-03"


@pytest.fixture
def owner(tmp_path, monkeypatch) -> Any:
    spec = importlib.util.spec_from_file_location("mark_submitted_under_test", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    state = tmp_path / "cfp-state.json"
    state.write_text(
        json.dumps(
            {
                "_last_checked": "2026-11-02T12:00:00Z",
                "devsum-2027": {
                    "name": "DevSum 2027",
                    "status": "open",
                    "cfp_url": "https://sessionize.com/devsum-2027",
                    "conf_date": "2027-05-20",
                },
                "already": {
                    "name": "Already",
                    "status": "sent",
                    "user_actioned": True,
                    "cfp_url": "https://sessionize.com/already",
                },
                "scale-23x": {
                    "name": "SCaLE 23x",
                    "status": "expired",
                    "cfp_url": "https://www.socallinuxexpo.org/scale/23x/cfp",
                    "conf_date": "2026-03-05",
                },
                "southern-california-linux-expo-scale-24x-2027": {
                    "name": "SCaLE 24x",
                    "status": "open",
                    "cfp_url": "https://www.socallinuxexpo.org/scale/24x/cfp",
                    "conf_date": "2027-03-04",
                },
                "ocx-2027": {"name": "OCX 2027", "status": "open", "conf_date": "2027-04-14"},
            }
        )
    )
    monkeypatch.setattr(module, "STATE", state)
    monkeypatch.setattr(module, "INSTALLED_COMMIT", tmp_path / "absent.py")
    return module


class Gmail:
    def __init__(self, mails):
        self.mails = mails
        self.archived = []

    def __call__(self, method, path, *, params: Any = None, body=None):
        if method == "POST":
            assert body == {"removeLabelIds": ["INBOX"]}
            self.archived.append(path.split("/")[2])
            return {}
        if path == "/messages":
            query = params["q"]
            assert query.endswith(" in:inbox newer_than:30d")
            domain = query.split()[0].removeprefix("from:")
            hits = [m for m in self.mails if domain in m["from"] and "INBOX" in m["labels"]]
            return {"messages": [{"id": m["id"]} for m in hits]}
        mail = next(m for m in self.mails if m["id"] == path.rsplit("/", 1)[-1])
        return {
            "labelIds": mail["labels"],
            "payload": {
                "headers": [
                    {"name": "From", "value": mail["from"]},
                    {"name": "Subject", "value": mail["subject"]},
                ]
            },
        }


def mail(identifier, sender, subject, labels=("INBOX", "IMPORTANT")):
    return {"id": identifier, "from": sender, "subject": subject, "labels": list(labels)}


def saved(owner, slug):
    return json.loads(owner.STATE.read_text())[slug]


def test_sessionize_confirmations_mark_the_event_with_every_talk_and_archive(owner):
    api = Gmail(
        [
            mail("a1", "devsum-2027@events.sessionize.com", "Like It or Not, You're the CTO Now"),
            mail(
                "a2", "devsum-2027@events.sessionize.com", "The Agent That Didn't Know Who to Ask"
            ),
        ]
    )
    result = owner.run(api, TODAY)
    assert result == {"marked": ["devsum-2027"], "archived": 2, "unmatched": []}
    record = saved(owner, "devsum-2027")
    assert record["status"] == "sent" and record["user_actioned"] is True
    assert record["updated"] == TODAY and record["conf_date"] == "2027-05-20"
    assert record["submitted_talks"] == [
        "Like It or Not, You're the CTO Now",
        "The Agent That Didn't Know Who to Ask",
    ]
    assert sorted(api.archived) == ["a1", "a2"]


def test_an_event_missing_from_the_tracker_is_added_as_sent(owner):
    api = Gmail([mail("b1", "new-conf-2027@events.sessionize.com", "A Talk")])
    assert owner.run(api, TODAY)["marked"] == ["new-conf-2027"]
    record = saved(owner, "new-conf-2027")
    assert record["cfp_url"] == "https://sessionize.com/new-conf-2027"
    assert record["status"] == "sent" and record["name"] == "New Conf 2027"


def test_scale_marks_the_newest_edition_and_takes_the_title_after_the_colon(owner):
    api = Gmail(
        [
            mail(
                "c1",
                "info@socallinuxexpo.org",
                "Your session proposal has been received: The Case of the Missing Bottleneck",
            )
        ]
    )
    assert owner.run(api, TODAY)["marked"] == ["southern-california-linux-expo-scale-24x-2027"]
    record = saved(owner, "southern-california-linux-expo-scale-24x-2027")
    assert record["submitted_talks"] == ["The Case of the Missing Bottleneck"]
    assert saved(owner, "scale-23x")["status"] == "expired"


def test_oxford_abstracts_matches_by_conference_name(owner):
    api = Gmail(
        [
            mail(
                "d1",
                "no-reply@oxfordabstracts.com",
                "OCX27 - Thank you for submitting your proposal to speak at OCX",
            )
        ]
    )
    assert owner.run(api, TODAY)["marked"] == ["ocx-2027"]
    assert saved(owner, "ocx-2027")["submitted_talks"] == []


def test_a_sent_event_gains_a_later_talk_title(owner):
    state = json.loads(owner.STATE.read_text())
    state["already"]["submitted_talks"] = ["Talk A"]
    owner.STATE.write_text(json.dumps(state))
    api = Gmail([mail("e1", "already@events.sessionize.com", "Talk B")])
    assert owner.run(api, TODAY) == {"marked": ["already"], "archived": 1, "unmatched": []}
    assert saved(owner, "already")["submitted_talks"] == ["Talk A", "Talk B"]


def test_a_sent_event_with_that_title_is_not_rewritten_but_its_mail_leaves_the_inbox(owner):
    state = json.loads(owner.STATE.read_text())
    state["already"]["submitted_talks"] = ["Talk A"]
    owner.STATE.write_text(json.dumps(state))
    before = saved(owner, "already")
    api = Gmail([mail("e1", "already@events.sessionize.com", "Talk A")])
    assert owner.run(api, TODAY) == {"marked": [], "archived": 1, "unmatched": []}
    assert saved(owner, "already") == before


def test_archived_mail_is_never_read_again(owner):
    # Last night's confirmation, already archived, must not land on a newer edition.
    api = Gmail(
        [
            mail(
                "f1",
                "info@socallinuxexpo.org",
                "Your session proposal has been received: Old Talk",
                labels=("IMPORTANT",),
            )
        ]
    )
    assert owner.run(api, TODAY) == {"marked": [], "archived": 0, "unmatched": []}
    assert saved(owner, "southern-california-linux-expo-scale-24x-2027")["status"] == "open"


def test_an_oxford_abstracts_confirmation_for_another_conference_is_not_ocx(owner):
    api = Gmail(
        [
            mail(
                "f2",
                "no-reply@oxfordabstracts.com",
                "RSE26 - Thank you for submitting your proposal",
            )
        ]
    )
    assert owner.run(api, TODAY) == {
        "marked": [],
        "archived": 0,
        "unmatched": ["no-reply@oxfordabstracts.com"],
    }
    assert saved(owner, "ocx-2027")["status"] == "open"


def test_a_confirmation_with_no_matching_cfp_stays_in_the_inbox_and_is_reported(owner):
    state = json.loads(owner.STATE.read_text())
    del state["ocx-2027"]
    owner.STATE.write_text(json.dumps(state))
    api = Gmail([mail("g1", "no-reply@oxfordabstracts.com", "OCX27 - Thank you")])
    assert owner.run(api, TODAY) == {
        "marked": [],
        "archived": 0,
        "unmatched": ["no-reply@oxfordabstracts.com"],
    }
    assert api.archived == []


def test_a_failed_state_write_archives_nothing(owner, monkeypatch):
    api = Gmail([mail("h1", "devsum-2027@events.sessionize.com", "A Talk")])

    def refuse(_updates):
        raise RuntimeError("commit-state failed")

    monkeypatch.setattr(owner, "commit", refuse)
    with pytest.raises(RuntimeError):
        owner.run(api, TODAY)
    assert api.archived == []


def test_an_approved_or_auto_dismissed_cfp_flips_to_sent(owner):
    # Live shape from 2026-11-02: a blocked-prefix auto-dismissal the owner submitted to anyway.
    state = json.loads(owner.STATE.read_text())
    state["devopsdays-zurich-2027"] = {
        "status": "dismissed",
        "cfp_url": "https://sessionize.com/devopsdays-zurich-2027",
        "bot_notes": "Auto-dismissed: blocked prefix 'devopsdays'",
    }
    state["approved-conf"] = {
        "status": "approved",
        "user_actioned": True,
        "cfp_url": "https://sessionize.com/approved-conf",
    }
    owner.STATE.write_text(json.dumps(state))
    api = Gmail(
        [
            mail("i1", "devopsdays-zurich-2027@events.sessionize.com", "A Talk"),
            mail("i2", "approved-conf@events.sessionize.com", "A Talk"),
        ]
    )
    assert owner.run(api, TODAY)["marked"] == ["approved-conf", "devopsdays-zurich-2027"]
    assert saved(owner, "devopsdays-zurich-2027")["status"] == "sent"
    assert saved(owner, "approved-conf")["status"] == "sent"
