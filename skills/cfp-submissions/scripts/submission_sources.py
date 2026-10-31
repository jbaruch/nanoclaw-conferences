"""Pure bounded adapters for rendered My Events and maintained mail evidence.

These adapters make no network calls and authenticate no caller or source.
Sessionize input is the rendered-node capture documented beside the skill.
Mail input is the unchanged Admin search-mail schema1 envelope with complete
body reads and their bound message dates. Unknown templates remain unresolved.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_NODES = 20000
MAX_MESSAGES = 1000
MAX_BODY = 256 * 1024
STATUS = {
    ("session-status_0", "Accepted"): "accepted",
    ("session-status_2", "Waitlisted"): "waitlisted",
    ("session-status_10", "In Evaluation"): "in_evaluation",
    ("session-status_20", "Declined"): "declined",
}
MONTHS = dict(
    zip("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), range(1, 13), strict=True)
)
LONG_MONTHS: dict[str, int] = dict(
    zip(
        (
            "January February March April May June July August September October November December"
        ).split(),
        range(1, 13),
        strict=True,
    )
)
FAMILIES = {
    "sessionize-submission",
    "sessionize-sessions",
    "pretalx",
    "oxford",
    "cfpdev",
    "devopsdays",
    "cfpdev-proposal",
}


class SourceError(ValueError):
    """A malformed/unsupported capture supplies no complete collection proof."""


def require(condition, code):
    if not condition:
        raise SourceError(f"{code}; retain the capture and use the documented source owner")


def bounded(value, size=2000) -> str:
    if not isinstance(value, str) or len(value) > size:
        raise SourceError("source_text; supply the documented bounded source text")
    require(not any(ord(c) < 32 and c not in "\n\r\t" for c in value), "source_text")
    return value


def list_value(value, code) -> list:
    if not isinstance(value, list):
        raise SourceError(f"{code}; supply the documented complete list")
    return value


def sha(value):
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
    ).hexdigest()


def new_fact(kind, identifier, observed_at, **fields):
    fact = dict.fromkeys(
        [
            "source_at",
            "event_name",
            "talk_title",
            "status",
            "submitted_on",
            "dates",
            "city",
            "co_speakers",
            "cfp_url",
        ]
    )
    fact.update(
        schema_version=1,
        evidence_id=identifier,
        kind=kind,
        citation={"kind": kind, "id": identifier, "url": None},
        observed_at=observed_at,
        complete=True,
    )
    fact.update(fields)
    return fact


def scope_record(source, scope, observed_at, gaps):
    gaps = sorted(set(gaps))
    return {
        "schema_version": 1,
        "id": f"{source}:{sha(scope)}",
        "source": source,
        "scope": scope,
        "checked_at": observed_at,
        "complete": not gaps,
        "gaps": gaps,
    }


def event_dates(value):
    """Only the observed explicit English list grammar; no natural date guesses."""
    match = re.fullmatch(r"(\d{1,2}) ([A-Z][a-z]{2}) - (\d{1,2}) ([A-Z][a-z]{2}) (\d{4})", value)
    if not match:
        return None
    first, month1, last, month2, year = match.groups()
    if month1 not in MONTHS or month2 not in MONTHS:
        return None
    try:
        start = date(int(year), MONTHS[month1], int(first))
        end = date(int(year), MONTHS[month2], int(last))
    except ValueError:
        return None
    return {"start": start.isoformat(), "end": end.isoformat()} if start <= end else None


def mail_date_range(first, last, month, year):
    if month not in LONG_MONTHS:
        return None
    try:
        start = date(int(year), LONG_MONTHS[month], int(first))
        end = date(int(year), LONG_MONTHS[month], int(last))
    except ValueError:
        return None
    if start > end:
        return None
    return {"start": start.isoformat(), "end": end.isoformat()}


def oxford_claim(value):
    match = re.fullmatch(r"(\d{1,2})-(\d{1,2}) ([A-Z][a-z]+) (\d{4}) in (.+)", value)
    if match is None:
        return {}
    dates = mail_date_range(*match.groups()[:4])
    if not dates:
        return {}
    return {"dates": dates, "city": match[5]}


def own_form_claim(value):
    match = re.fullmatch(r".+ (\d{4}) (\d{1,2})-(\d{1,2}) ([A-Z][a-z]+)", value)
    return mail_date_range(match[2], match[3], match[4], match[1]) if match else None


def observation_clock(value):
    bounded(value, 40)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise SourceError("source_time; preserve the actual zoned observation clock") from error
    require("T" in value and parsed.tzinfo is not None, "source_time")
    return parsed


def native_path(value, pattern):
    if not isinstance(value, str):
        return None
    try:
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "sessionize.com"
            or parsed.query
            or parsed.fragment
        ):
            return None
        match = re.fullmatch(pattern, parsed.path)
        return match.group(1) if match else None
    except ValueError:
        return None


def sessionize(request):
    """Return observations and scope coverage from the actual rendered tree."""
    require(set(request) == {"schema_version", "source", "observed_at", "capture"}, "capture_shape")
    capture = request["capture"]
    require(
        isinstance(capture, dict)
        and type(capture.get("schema_version")) is int
        and capture["schema_version"] == 1,
        "capture_version",
    )
    require(
        capture.get("origin") == "https://sessionize.com"
        and capture.get("path") == "/app/speaker/events",
        "capture_route",
    )
    nodes = list_value(capture.get("nodes"), "capture_bounds")
    require(1 <= len(nodes) <= MAX_NODES, "capture_bounds")
    children = [[] for _ in nodes]
    for i, node in enumerate(nodes):
        require(isinstance(node, dict), "capture_node")
        bounded(node.get("text"), MAX_BODY)
        bounded(node.get("classes"), 1000)
        bounded(node.get("tag"), 20)
        parent = node.get("parent")
        require(
            (i == 0 and parent is None) or (type(parent) is int and 0 <= parent < i), "capture_tree"
        )
        if parent is not None:
            children[parent].append(i)

    def descendants(index):
        result, pending = [], [index]
        while pending:
            current = pending.pop()
            result.append(current)
            pending.extend(reversed(children[current]))
        return result

    def classes(index):
        return set(nodes[index]["classes"].split())

    account = [n["text"] for n in nodes if "event-title" in n["classes"].split() and n["text"]]
    require(len(account) == 1, "capture_account")
    active = [
        i
        for i, n in enumerate(nodes)
        if n["tag"] == "LI"
        and "active" in classes(i)
        and any(nodes[j]["text"] == "Current events" for j in children[i])
    ]
    headings = [
        i for i, n in enumerate(nodes) if n["tag"] == "H3" and n["text"] == "Current events"
    ]
    require(len(active) == 1 and len(headings) == 1, "capture_filter")
    counts = [nodes[j]["text"] for j in children[headings[0]] if nodes[j]["tag"] == "SMALL"]
    require(len(counts) == 1 and re.fullmatch(r"\(\d+\)", counts[0]), "capture_count")
    declared = int(counts[0][1:-1])
    tables = [i for i, n in enumerate(nodes) if n["tag"] == "TBODY"]
    require(len(tables) == 1, "capture_table")
    rows = children[tables[0]]
    require(all(nodes[i]["tag"] == "TR" for i in rows), "capture_rows")
    gaps, observations, event_count, current, seen = [], [], 0, None, set()
    seen_events = set()
    account_scope = f"visible-account-sha256:{sha(account[0])}; Current events; /app/speaker/events"
    if any("pagination" in n["classes"].split() for n in nodes):
        gaps.append("pagination controls present; additional pages not verified")

    def finish_event():
        if current is not None and current["expected"] != current["count"]:
            gaps.append("declared session count does not match visible session rows")

    for row_index in rows:
        inside = descendants(row_index)
        if "event" in classes(row_index):
            finish_event()
            event_count += 1
            links = [
                (nodes[i], native_path(nodes[i].get("href"), r"/app/speaker/event/details/(\d+)"))
                for i in inside
                if nodes[i]["tag"] == "A"
            ]
            links = [(n, identifier) for n, identifier in links if identifier]
            require(len(links) == 1, "capture_event_identity")
            node, event_id = links[0]
            require(event_id not in seen_events, "capture_duplicate_event")
            seen_events.add(event_id)
            bounded(node["text"], 300)
            dates = [event_dates(nodes[i]["text"]) for i in inside if nodes[i]["tag"] == "SMALL"]
            dates = dates[0] if len(dates) == 1 else None
            if dates is None:
                gaps.append("event date grammar missing or unsupported")
            summaries = [
                nodes[i]["text"]
                for i in inside
                if re.fullmatch(r"(?:\d+/)?\d+ (?:submitted|accepted)", nodes[i]["text"])
            ]
            require(len(summaries) == 1, "capture_session_count")
            count = int(summaries[0].split()[0].split("/")[-1])
            current = {
                "event_id": event_id,
                "event_name": node["text"] or None,
                "dates": dates,
                "expected": count,
                "count": 0,
            }
            continue
        if current is None:
            raise SourceError("capture_session_parent; retain the complete event/session tree")
        current["count"] += 1
        links = [
            (
                nodes[i],
                native_path(nodes[i].get("href"), r"/app/speaker/session/([A-Za-z0-9_-]{1,100})"),
            )
            for i in inside
            if nodes[i]["tag"] == "A"
        ]
        links = [(n, identifier) for n, identifier in links if identifier]
        require(len(links) == 1, "capture_proposal_identity")
        node, proposal = links[0]
        pair = (current["event_id"], proposal)
        require(pair not in seen, "capture_duplicate_proposal")
        seen.add(pair)
        status_nodes = [
            i for i in inside if any(c.startswith("session-status_") for c in classes(i))
        ]
        statuses = (
            [
                STATUS[(c, nodes[i]["text"])]
                for i in status_nodes
                for c in classes(i)
                if (c, nodes[i]["text"]) in STATUS
            ]
            if len(status_nodes) == 1
            else []
        )
        status = statuses[0] if len(statuses) == 1 else None
        if status is None:
            gaps.append("session status missing or unsupported")
        fact = new_fact(
            "sessionize",
            "pending",
            request["observed_at"],
            event_name=current["event_name"],
            talk_title=node["text"] or None,
            status=status,
            dates=current["dates"],
        )
        fact["citation"] = {"kind": "sessionize", "id": proposal, "url": node["href"]}
        fact["evidence_id"] = f"sessionize:{current['event_id']}:{proposal}:{sha(fact)}"
        observations.append(
            {
                "identity": {
                    "event": {"namespace": "sessionize", "id": pair[0]},
                    "talk": {"namespace": "sessionize", "id": pair[1]},
                },
                "fact": fact,
            }
        )
    finish_event()
    if declared != event_count:
        gaps.append("declared event count does not match visible event rows")
    return observations, [scope_record("sessionize", account_scope, request["observed_at"], gaps)]


class VisibleMail(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def mail_text(body):
    bounded(body, MAX_BODY)
    parser = VisibleMail()
    parser.feed(body)
    # No edit link (including its bearer path) reaches a retained field.
    return " ".join(re.sub(r"https?://\S+", " ", " ".join(parser.parts)).split())


def mail_fields(family, subject, body):
    """Only actual observed structural templates establish positive claims."""
    result = {}
    if family in {"pretalx", "devopsdays"} and subject.startswith("Your proposal: "):
        match = re.search(
            r"We have received your proposal [“\"](.+?)[”\"] to (.+?)\. We will notify you", body
        )
        if match and subject.removeprefix("Your proposal: ") == match[1]:
            result.update(talk_title=match[1], event_name=match[2], status="submitted")
    elif family == "devopsdays":
        match = re.search(
            r'Thank you for submitting "(.+?)" to (.+?)\. You can view, edit, or withdraw', body
        )
        if match and subject == f"{match[2]} We received your proposal: {match[1]}":
            result.update(
                talk_title=match[1],
                event_name=match[2],
                status="submitted",
                dates=own_form_claim(match[2]),
            )
    elif family == "oxford":
        match = re.search(
            r"Thank you for submitting your proposal to speak at (.+?) ,?\s*from (.+?)\. "
            r"We will review completed proposals",
            body,
        )
        if (
            match
            and "Thank you for submitting your proposal" in subject
            and "Oxford Abstracts" in body
        ):
            result.update(event_name=match[1], status="submitted", **oxford_claim(match[2]))
    elif family == "sessionize-sessions" and subject.startswith("Your sessions at "):
        if (
            "following sessions have not been accepted for " in body
            or "unable to include your session(s) in the final program of " in body
        ):
            result.update(event_name=subject.removeprefix("Your sessions at "), status="declined")
    elif family == "cfpdev-proposal":
        refused = re.fullmatch(r"\[[A-Za-z0-9_-]+\] Your proposal has been refused : (.+)", subject)
        accepted = re.fullmatch(
            r"\[[A-Za-z0-9_-]+\] Your proposal has been accepted : (.+)", subject
        )
        backup = re.fullmatch(r"\[[A-Za-z0-9_-]+\] Your proposal (.+) is marked as backup", subject)
        proposal = re.search(
            r'Thank you for submitting your proposal "(.+?)" (?:for|to) (.+?)\.', body
        )
        decision = re.search(r'proposal, "(.+?)", has been accepted for (.+?)!', body)
        if (
            refused
            and proposal
            and refused[1] == proposal[1]
            and "your talk was not selected" in body
        ):
            result.update(talk_title=proposal[1], event_name=proposal[2], status="declined")
        elif accepted and decision and accepted[1] == decision[1]:
            result.update(talk_title=decision[1], event_name=decision[2], status="accepted")
        elif (
            backup
            and proposal
            and backup[1] == proposal[1]
            and (
                "Your proposal has been selected as a backup talk." in body
                and "standby lineup should scheduling changes occur" in body
            )
        ):
            result.update(talk_title=proposal[1], event_name=proposal[2], status="waitlisted")
    return result


def mail(request):
    require(set(request) == {"schema_version", "source", "collections"}, "mail_capture_shape")
    collections = request["collections"]
    require(isinstance(collections, list) and 1 <= len(collections) <= 20, "mail_capture_bounds")
    observations, scopes, seen, count = [], [], {}, 0
    for capture in collections:
        require(
            isinstance(capture, dict)
            and set(capture) == {"family", "scope", "observed_at", "evidence"},
            "mail_capture_shape",
        )
        family = capture["family"]
        require(isinstance(family, str) and family in FAMILIES, "mail_family")
        scope = bounded(capture["scope"])
        observed_at = bounded(capture["observed_at"], 40)
        clock = observation_clock(observed_at)
        evidence = capture["evidence"]
        require(
            isinstance(evidence, dict)
            and type(evidence.get("schema_version")) is int
            and evidence["schema_version"] == 1,
            "mail_evidence_version",
        )
        threads = list_value(evidence.get("threads"), "mail_threads")
        require(len(threads) <= MAX_MESSAGES, "mail_threads")
        require(
            type(evidence.get("complete")) is bool and isinstance(evidence.get("errors"), list),
            "mail_coverage",
        )
        gaps = []
        if (
            not evidence["complete"]
            or evidence.get("next_page_token")
            or evidence.get("threads_omitted")
            or evidence["errors"]
        ):
            gaps.append("search incomplete, paginated, omitted or failed")
        if not threads:
            gaps.append("no source template observed; empty search is bounded evidence only")
        for thread in threads:
            require(
                isinstance(thread, dict) and isinstance(thread.get("messages"), list),
                "mail_messages",
            )
            require(type(thread.get("complete")) is bool, "mail_thread_coverage")
            if not thread["complete"] or thread.get("messages_omitted"):
                gaps.append("thread incomplete or messages omitted")
            for message in thread["messages"]:
                count += 1
                require(count <= MAX_MESSAGES and isinstance(message, dict), "mail_message_bounds")
                identifier = bounded(message.get("messageId"), 100)
                require(re.fullmatch(r"[A-Za-z0-9_-]{1,100}", identifier), "mail_message_identity")
                subject = bounded(message.get("subject"), 2000)
                source_at = bounded(message.get("internalDate"), 40)
                require(type(message.get("body_complete")) is bool, "mail_body_coverage")
                raw_body = bounded(message.get("body"), MAX_BODY)
                fingerprint = sha([subject, source_at, raw_body, message["body_complete"]])
                fields = mail_fields(family, subject, mail_text(raw_body))
                if not fields:
                    gaps.append("source template unsupported; no status inferred")
                gaps.append("proposal identity unverified; explicit owner/native binding required")
                if not message["body_complete"]:
                    gaps.append("message body incomplete")
                prior = seen.get(identifier)
                if prior is None:
                    seen[identifier] = {
                        "fingerprint": fingerprint,
                        "fields": fields,
                        "clock": clock,
                        "observed_at": observed_at,
                        "source_at": source_at,
                        "complete": message["body_complete"],
                    }
                else:
                    require(prior["fingerprint"] == fingerprint, "mail_identity_conflict")
                    for field, value in fields.items():
                        require(
                            field not in prior["fields"] or prior["fields"][field] == value,
                            "mail_template_conflict",
                        )
                        prior["fields"][field] = value
                    if (clock, observed_at) < (prior["clock"], prior["observed_at"]):
                        prior.update(clock=clock, observed_at=observed_at)
        scopes.append(scope_record(f"mail:{family}", scope, capture["observed_at"], gaps))
    for identifier, item in sorted(seen.items()):
        fact = new_fact(
            "mail",
            f"mail:{identifier}:{sha([item['fingerprint'], item['observed_at'], item['fields']])}",
            item["observed_at"],
            source_at=item["source_at"],
            complete=item["complete"],
            **item["fields"],
        )
        fact["citation"] = {"kind": "mail", "id": identifier, "url": None}
        observations.append({"identity": None, "fact": fact})
    return observations, scopes


def prepare(request):
    require(
        isinstance(request, dict)
        and type(request.get("schema_version")) is int
        and request["schema_version"] == 1,
        "source_input_version",
    )
    if request.get("source") == "sessionize":
        return sessionize(request)
    if request.get("source") == "mail":
        return mail(request)
    raise SourceError("source_kind; use sessionize or mail and the documented capture owner")
