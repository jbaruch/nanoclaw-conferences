"""Pure bounded adapters for rendered My Events and maintained mail evidence.

These adapters make no network calls and authenticate no caller or source.
Sessionize input is the rendered-node capture documented beside the skill.
Mail source1 retains Admin search-mail schema1 unchanged. Mail source2 accepts
the versioned Admin mail-evidence wrapper and joins bound full-body reads without
repairing search coverage. Unknown templates remain unresolved.

Callable contract: prepare(request) accepts one decoded source1 dict with source
sessionize or mail, or a mail source2 dict, as defined in references/source-input.md. It returns the
tuple (observations, scopes): fresh lists of identity/fact observations for the
ledger owner and coverage1 records describing checked scopes and retained gaps.
Unknown templates or missing identities remain unresolved observations; they do
not establish absence. Malformed, unsupported, oversized or contradictory input
raises SourceError with a bounded recovery diagnostic before any caller commit.
Unexpected implementation errors propagate. This module performs no filesystem,
network, subprocess, ledger or CFP I/O and does not authenticate caller evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_NODES = 12000
MAX_CAPTURE_BYTES = 1_500_000
SESSIONIZE_SCOPE = (
    "Complete rendered node tree of the observed My Events view; forms, hidden content, "
    "credential/storage values and unsafe links excluded. Account/filter/history completeness "
    "remains unknown until consumer validation."
)
MAX_MESSAGES = 1000
MAX_BODY = 256 * 1024
MAX_CLAIM_TEXT = 300
# Published Admin wrapper1 and unchanged search1/fetch1 bounds.
MAX_MAIL_WRAPPER_BYTES = 512 * 1024
MAX_MAIL_SEARCH_BYTES = 64 * 1024
MAX_MAIL_READS = 50
MAX_MAIL_THREADS = 30
MAIL_READ_ERRORS = {
    "binding_changed",
    "body_unavailable",
    "body_incomplete",
    "body_too_large",
    "aggregate_limit",
}
MAIL_ID = re.compile(r"[A-Fa-f0-9]{1,128}")
# Non-Cc/Cf default-ignorable ranges from Unicode DerivedCoreProperties.
# Unknown/unassigned characters are unsupported rather than guessed visible.
DEFAULT_IGNORABLE_TEXT = re.compile(
    r"[\u034f\u115f-\u1160\u17b4-\u17b5\u180b-\u180d\u180f"
    r"\u3164\ufe00-\ufe0f\uffa0\U000e0100-\U000e01ef]"
)
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


def native_string(value, maximum):
    require(isinstance(value, str), "capture_node")
    try:
        units = len(value.encode("utf-16-le")) // 2
    except UnicodeEncodeError as error:
        raise SourceError("capture_encoding; retain unsupported native text") from error
    require(units <= maximum, "capture_node_bounds")
    return value


def capture_node(node):
    required = {"tag", "parent", "classes", "text"}
    require(
        isinstance(node, dict) and required <= set(node) <= required | {"href", "link_omitted"},
        "capture_node_shape",
    )
    tag = native_string(node["tag"], 20)
    require(re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,19}", tag), "capture_node_tag")
    native_string(node["classes"], 1000)
    value = native_string(node["text"], 16000)
    require(
        value == " ".join(value.split())
        and not DEFAULT_IGNORABLE_TEXT.search(value)
        and not any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Cn"} for c in value),
        "capture_node_text",
    )
    if "href" in node:
        require(
            isinstance(node["href"], str)
            and re.fullmatch(
                r"https://sessionize\.com/app/speaker/"
                r"(?:event/details/[0-9]+|session/[A-Za-z0-9_-]{1,100})",
                node["href"],
            ),
            "capture_node_link",
        )
    require(
        ("link_omitted" not in node or node["link_omitted"] is True)
        and not ("href" in node and "link_omitted" in node)
        and (not ({"href", "link_omitted"} & set(node)) or tag.upper() == "A"),
        "capture_node_link",
    )


def list_value(value, code) -> list:
    if not isinstance(value, list):
        raise SourceError(f"{code}; supply the documented complete list")
    return value


def sha(value):
    try:
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        ).hexdigest()
    except (UnicodeEncodeError, ValueError) as error:
        raise SourceError("source_encoding; supply finite UTF-8 source data") from error


def material(fact):
    """Immutable claim content; adapter last-seen metadata is not a source clock."""
    return {key: value for key, value in fact.items() if key not in {"evidence_id", "observed_at"}}


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


def sessionize_id(identity, fact):
    if (
        not isinstance(identity, dict)
        or fact.get("kind") != "sessionize"
        or fact.get("source_at") is not None
    ):
        return None
    event, talk = identity.get("event"), identity.get("talk")
    if not isinstance(event, dict) or not isinstance(talk, dict):
        return None
    if event.get("namespace") != "sessionize" or talk.get("namespace") != "sessionize":
        return None
    event_id, proposal = event.get("id"), talk.get("id")
    if not isinstance(event_id, str) or not re.fullmatch(r"\d+", event_id):
        return None
    citation = fact.get("citation")
    if (
        not isinstance(citation, dict)
        or citation.get("kind") != "sessionize"
        or citation.get("id") != proposal
    ):
        return None
    if native_path(citation.get("url"), r"/app/speaker/session/([A-Za-z0-9_-]{1,100})") != proposal:
        return None
    return f"sessionize:{event_id}:{proposal}:{sha([identity, material(fact)])}"


def mail_id(fingerprint, fact):
    return f"mail:{fact['citation']['id']}:{fingerprint}:{sha(material(fact))}"


def adapter_kind(identity, fact):
    """Recognize an exact content-bound adapter ID, never actor/source authentication."""
    identifier = fact.get("evidence_id")
    if sessionize_id(identity, fact) == identifier and identifier is not None:
        return "sessionize"
    if fact.get("kind") == "mail" and isinstance(identifier, str):
        match = re.fullmatch(
            rf"mail:({MAIL_ID.pattern}):([a-f0-9]{{64}}):[a-f0-9]{{64}}", identifier
        )
        citation = fact.get("citation")
        if (
            match
            and isinstance(citation, dict)
            and citation.get("kind") == "mail"
            and citation.get("id") == match[1]
            and citation.get("url") is None
            and mail_id(match[2], fact) == identifier
        ):
            return "mail"
    return None


def rendered_count(value):
    try:
        return int(value)
    except ValueError as error:
        raise SourceError(
            "capture_count; retain the capture and inspect its unsupported rendered count"
        ) from error


def sessionize(request):
    """Return observations and scope coverage from the actual rendered tree."""
    require(set(request) == {"schema_version", "source", "observed_at", "capture"}, "capture_shape")
    capture = request["capture"]
    require(
        isinstance(capture, dict)
        and set(capture) == {"schema_version", "origin", "path", "title", "nodes", "scope"},
        "capture_shape",
    )
    require(
        native_string(capture["title"], 300).strip() and capture["scope"] == SESSIONIZE_SCOPE,
        "capture_provenance",
    )
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
        capture_node(node)
        parent = node.get("parent")
        require(
            (i == 0 and parent is None and node["tag"] == "BODY")
            or (type(parent) is int and 0 <= parent < i),
            "capture_tree",
        )
        if parent is not None:
            children[parent].append(i)
    # Host checks JSON.stringify(dom) before attaching its scope metadata.
    # Validated native values have no floating-point or unsupported string forms.
    native_dom = {key: value for key, value in capture.items() if key != "scope"}
    require(
        len(json.dumps(native_dom, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= MAX_CAPTURE_BYTES,
        "capture_bounds",
    )

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
    declared = rendered_count(counts[0][1:-1])
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
            count = rendered_count(summaries[0].split()[0].split("/")[-1])
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
        identity = {
            "event": {"namespace": "sessionize", "id": pair[0]},
            "talk": {"namespace": "sessionize", "id": pair[1]},
        }
        fact["evidence_id"] = sessionize_id(identity, fact)
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


def template_pair(body, patterns, suffix):
    """Fixed-count delimiter reads; unrecognized/oversized fields remain unknown."""
    for prefix, separator in patterns:
        start = body.find(prefix)
        if start < 0:
            continue
        start += len(prefix)
        split = body.find(separator, start)
        if split < 0 or not 0 < split - start <= MAX_CLAIM_TEXT:
            continue
        second = split + len(separator)
        end = body.find(suffix, second)
        if end < 0 or not 0 < end - second <= MAX_CLAIM_TEXT:
            continue
        return body[start:split], body[second:end]
    return None


def mail_fields(family, subject, body):
    """Only actual observed structural templates establish positive claims."""
    result = {}
    # Query families describe coverage, not which claim a fixed template means.
    if subject.startswith("Your proposal: "):
        match = template_pair(
            body,
            [
                ("We have received your proposal “", "” to "),
                ('We have received your proposal "', '" to '),
            ],
            ". We will notify you",
        )
        if match and subject.removeprefix("Your proposal: ") == match[0]:
            result.update(talk_title=match[0], event_name=match[1], status="submitted")
    elif " We received your proposal: " in subject:
        match = template_pair(
            body,
            [('Thank you for submitting "', '" to ')],
            ". You can view, edit, or withdraw",
        )
        if match and subject == f"{match[1]} We received your proposal: {match[0]}":
            result.update(
                talk_title=match[0],
                event_name=match[1],
                status="submitted",
                dates=own_form_claim(match[1]),
            )
    elif "Thank you for submitting your proposal" in subject:
        match = template_pair(
            body,
            [
                ("Thank you for submitting your proposal to speak at ", " , from "),
                ("Thank you for submitting your proposal to speak at ", " from "),
            ],
            ". We will review completed proposals",
        )
        if (
            match
            and "Thank you for submitting your proposal" in subject
            and "Oxford Abstracts" in body
        ):
            result.update(event_name=match[0], status="submitted", **oxford_claim(match[1]))
    elif subject.startswith("Your sessions at "):
        if (
            "following sessions have not been accepted for " in body
            or "unable to include your session(s) in the final program of " in body
        ):
            name = " ".join(subject.removeprefix("Your sessions at ").split())
            result.update(
                event_name=name if 0 < len(name) <= MAX_CLAIM_TEXT else None, status="declined"
            )
    elif re.match(r"\[[A-Za-z0-9_-]+\] Your proposal ", subject):
        refused = re.fullmatch(r"\[[A-Za-z0-9_-]+\] Your proposal has been refused : (.+)", subject)
        accepted = re.fullmatch(
            r"\[[A-Za-z0-9_-]+\] Your proposal has been accepted : (.+)", subject
        )
        backup = re.fullmatch(r"\[[A-Za-z0-9_-]+\] Your proposal (.+) is marked as backup", subject)
        proposal = template_pair(
            body,
            [
                ('Thank you for submitting your proposal "', '" for '),
                ('Thank you for submitting your proposal "', '" to '),
            ],
            ".",
        )
        decision = template_pair(body, [('proposal, "', '", has been accepted for ')], "!")
        if (
            refused
            and proposal
            and refused[1] == proposal[0]
            and "your talk was not selected" in body
        ):
            result.update(talk_title=proposal[0], event_name=proposal[1], status="declined")
        elif accepted and decision and accepted[1] == decision[0]:
            result.update(talk_title=decision[0], event_name=decision[1], status="accepted")
        elif (
            backup
            and proposal
            and backup[1] == proposal[0]
            and (
                "Your proposal has been selected as a backup talk." in body
                and "standby lineup should scheduling changes occur" in body
            )
        ):
            result.update(talk_title=proposal[0], event_name=proposal[1], status="waitlisted")
    return result


def mail_bytes(value):
    try:
        return len(
            json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
        )
    except (ValueError, UnicodeEncodeError) as error:
        raise SourceError("mail_encoding; retain the unsupported provider evidence") from error


def mail_header(value):
    """Admin sanitization retains its fixed marker after a cut header."""
    result = bounded(value, 306)
    require(len(result) <= 300 or len(result) == 306 and result.endswith(" [...]"), "mail_header")
    return result


def mail_search(search):
    """Validate the published search1 envelope, including nested metadata.

    Original coverage remains authoritative; a false flag need not have a
    visible gap (the invocation may have started at a continuation token).
    Latest-message metadata can name an omitted message, never an invented
    complete retained inventory. Values are read unchanged, not normalized.
    """
    require(
        isinstance(search, dict)
        and set(search)
        == {"schema_version", "threads", "next_page_token", "threads_omitted", "complete", "errors"}
        and type(search["schema_version"]) is int
        and search["schema_version"] == 1,
        "mail_search_version",
    )
    require(mail_bytes(search) <= MAX_MAIL_SEARCH_BYTES, "mail_search_bounds")
    require(
        type(search["complete"]) is bool
        and type(search["threads_omitted"]) is int
        and search["threads_omitted"] >= 0
        and (
            search["next_page_token"] is None
            or isinstance(search["next_page_token"], str)
            and 0 < len(search["next_page_token"].encode()) <= 2000
        ),
        "mail_search_coverage",
    )
    simple_errors = {
        "page_limit_exceeded",
        "page_token_too_large",
        "repeated_page_token",
        "invalid_thread_id",
    }
    thread_errors = {
        "thread_unavailable",
        "thread_shape",
        "thread_identity",
        "malformed_payload",
        "malformed_thread",
    }
    errors = list_value(search["errors"], "mail_search_errors")
    failed_stubs: dict[str, set[str]] = {}
    invalid_stubs = 0
    page_limits = 0
    terminal_page = False
    stubs_started = False
    for index, error in enumerate(errors):
        require(isinstance(error, dict) and isinstance(error.get("kind"), str), "mail_search_error")
        kind = error["kind"]
        require(
            kind in simple_errors
            and set(error) == {"kind"}
            or kind in thread_errors
            and set(error) == {"thread_id", "kind"}
            and isinstance(error["thread_id"], str)
            and MAIL_ID.fullmatch(error["thread_id"]),
            "mail_search_error",
        )
        if kind in {"page_limit_exceeded", "page_token_too_large", "repeated_page_token"}:
            require(not stubs_started and not terminal_page, "mail_search_error")
            if kind == "page_limit_exceeded":
                page_limits += 1
                require(page_limits <= 3, "mail_search_error")
            else:
                terminal_page = True
                require(
                    (search["next_page_token"] is None) == (kind == "page_token_too_large"),
                    "mail_search_error",
                )
        else:
            stubs_started = True
        if kind == "invalid_thread_id":
            invalid_stubs += 1
        elif kind in thread_errors:
            stages = failed_stubs.setdefault(error["thread_id"], set())
            # The producer may record malformed payload before parsing that
            # same stub also fails. Other failure paths stop at their stage.
            require(
                not stages
                or stages == {"malformed_payload"}
                and kind == "malformed_thread"
                and errors[index - 1]
                == {"kind": "malformed_payload", "thread_id": error["thread_id"]},
                "mail_search_error",
            )
            stages.add(kind)
    threads = list_value(search["threads"], "mail_threads")
    # Reject oversized retained/omitted inventory before traversing its content;
    # failed stub slots are included after the retained identities are known.
    require(len(threads) + search["threads_omitted"] <= MAX_MAIL_THREADS, "mail_threads")
    originals, pending, tids = {}, {}, set()
    gap = bool(errors or search["threads_omitted"] or search["next_page_token"])
    for thread in threads:
        require(
            isinstance(thread, dict)
            and set(thread)
            == {
                "thread_id",
                "citation_url",
                "source_trust",
                "messages",
                "message_count",
                "complete",
                "thread_latest_message_from",
                "thread_latest_message_id",
                "thread_latest_message_date",
                "messages_omitted",
            },
            "mail_thread_shape",
        )
        tid = bounded(thread["thread_id"], 128)
        require(MAIL_ID.fullmatch(tid) and tid not in tids, "mail_thread_identity")
        tids.add(tid)
        require(
            thread["citation_url"] == f"https://mail.google.com/mail/u/0/#all/{tid}"
            and thread["source_trust"] == "untrusted",
            "mail_thread_binding",
        )
        messages = list_value(thread["messages"], "mail_messages")
        require(
            type(thread["message_count"]) is int
            and thread["message_count"] > 0
            and type(thread["messages_omitted"]) is int
            and thread["messages_omitted"] >= 0
            and thread["message_count"] == len(messages) + thread["messages_omitted"]
            and type(thread["complete"]) is bool,
            "mail_thread_coverage",
        )
        dated, undated, body_gap = [], False, False
        for message in messages:
            require(
                isinstance(message, dict)
                and set(message)
                == {
                    "messageId",
                    "from",
                    "to",
                    "subject",
                    "internalDate",
                    "labelIds",
                    "body",
                    "body_complete",
                },
                "mail_message_shape",
            )
            mid = bounded(message["messageId"], 128)
            require(MAIL_ID.fullmatch(mid) and mid not in originals, "mail_message_identity")
            for key in ("from", "to", "subject"):
                mail_header(message[key])
            labels = list_value(message["labelIds"], "mail_message_labels")
            require(
                all(isinstance(label, str) and 0 < len(label) <= 200 for label in labels),
                "mail_message_labels",
            )
            require(type(message["body_complete"]) is bool, "mail_body_coverage")
            bounded(message["body"], 6000)
            if message["internalDate"] is None:
                undated = True
            else:
                stamp = observation_clock(message["internalDate"])
                require(stamp.utcoffset() == timedelta(0), "mail_message_date")
                dated.append((stamp, mid, "me" if "SENT" in labels else "them"))
            originals[mid] = (tid, message)
            require(len(originals) <= MAX_MAIL_READS, "mail_message_bounds")
            body_gap |= not message["body_complete"]
            if not message["body_complete"]:
                pending[mid] = (tid, message)
        direction, latest_id, latest_date = (
            thread[key]
            for key in (
                "thread_latest_message_from",
                "thread_latest_message_id",
                "thread_latest_message_date",
            )
        )
        require(
            isinstance(direction, str)
            and direction in {"me", "them", "unknown"}
            and (latest_id is None or isinstance(latest_id, str) and MAIL_ID.fullmatch(latest_id)),
            "mail_latest_metadata",
        )
        if latest_date is None:
            require(latest_id is None and direction == "unknown", "mail_latest_metadata")
        else:
            latest = observation_clock(latest_date)
            require(
                latest.utcoffset() == timedelta(0)
                and not undated
                and all(item[0] <= latest for item in dated),
                "mail_latest_metadata",
            )
            if not thread["messages_omitted"]:
                newest = max(item[0] for item in dated)
                tied = [item for item in dated if item[0] == newest]
                directions = {item[2] for item in tied}
                require(
                    latest == newest
                    and latest_id == (tied[0][1] if len(tied) == 1 else None)
                    and direction
                    == (next(iter(directions)) if len(directions) == 1 else "unknown"),
                    "mail_latest_metadata",
                )
        if undated:
            require(latest_date is None, "mail_latest_metadata")
        elif not thread["messages_omitted"]:
            require(latest_date is not None, "mail_latest_metadata")
        malformed_payload = False
        for error in errors:
            if error.get("thread_id") == tid:
                malformed_payload = error["kind"] == "malformed_payload"
                require(
                    error["kind"] == "malformed_payload" and not thread["complete"],
                    "mail_thread_coverage",
                )
        if latest_id is not None:
            require(direction in {"me", "them"}, "mail_latest_metadata")
            retained = next((item for item in dated if item[1] == latest_id), None)
            require(retained is not None or thread["messages_omitted"] > 0, "mail_latest_metadata")
            if retained is not None:
                require(
                    latest_date is not None
                    and retained[0] == observation_clock(latest_date)
                    and retained[2] == direction,
                    "mail_latest_metadata",
                )
        thread_gap = undated or body_gap or bool(thread["messages_omitted"]) or malformed_payload
        require(thread["complete"] == (not thread_gap), "mail_thread_coverage")
        gap |= not thread["complete"] or thread_gap
    dropped_malformed = sum(
        tid not in tids and stages == {"malformed_payload"} for tid, stages in failed_stubs.items()
    )
    require(dropped_malformed <= search["threads_omitted"], "mail_search_coverage")
    failed = sum(
        tid not in tids and stages != {"malformed_payload"} for tid, stages in failed_stubs.items()
    )
    # A successfully parsed malformed thread can later be byte-cap omitted;
    # its malformed_payload error is already covered by threads_omitted. A
    # payload+parser failure consumes one distinct failed stub, not two.
    require(
        len(threads) + search["threads_omitted"] + invalid_stubs + failed <= MAX_MAIL_THREADS,
        "mail_threads",
    )
    require(not search["complete"] or not gap, "mail_search_coverage")
    return originals, pending


def mail_handoff(wrapper):
    """Validate wrapper1 before any import; return unchanged search1 and bound reads.

    Limits/error kinds above mirror the declared Admin mail-evidence owner.
    No data is authenticated here. Successful hydration can support a positive
    message fact; it never clears original search/thread/body coverage gaps.
    """
    require(
        isinstance(wrapper, dict)
        and set(wrapper)
        == {"schema_version", "search", "body_reads", "body_reads_omitted", "complete", "errors"}
        and type(wrapper["schema_version"]) is int
        and wrapper["schema_version"] == 1,
        "mail_handoff_version",
    )
    require(mail_bytes(wrapper) <= MAX_MAIL_WRAPPER_BYTES, "mail_handoff_bounds")
    search = wrapper["search"]
    _, pending = mail_search(search)
    reads, errors = {}, {}
    for read in list_value(wrapper["body_reads"], "mail_body_reads"):
        require(
            isinstance(read, dict)
            and set(read) == {"message_id", "thread_id", "internalDate", "evidence"},
            "mail_body_binding",
        )
        mid = bounded(read["message_id"], 128)
        require(mid in pending and mid not in reads, "mail_body_binding")
        tid, original = pending[mid]
        require(
            read["thread_id"] == tid
            and read["internalDate"] == original.get("internalDate")
            and isinstance(read["internalDate"], str)
            and read["internalDate"],
            "mail_body_binding",
        )
        full = read["evidence"]
        require(
            isinstance(full, dict)
            and set(full)
            == {
                "schema_version",
                "message_id",
                "thread_id",
                "subject",
                "from",
                "body",
                "body_complete",
                "citation_url",
                "source_trust",
            }
            and type(full["schema_version"]) is int
            and full["schema_version"] == 1
            and full["message_id"] == mid
            and full["thread_id"] == tid
            and full["citation_url"] == f"https://mail.google.com/mail/u/0/#all/{tid}"
            and full["source_trust"] == "untrusted"
            and type(full["body_complete"]) is bool,
            "mail_body_evidence",
        )
        mail_header(full["subject"])
        mail_header(full["from"])
        require(full["subject"] == original["subject"], "mail_body_binding")
        body = bounded(full["body"], MAX_BODY)
        require(len(body.encode()) <= MAX_BODY, "mail_body_bounds")
        reads[mid] = full
    for error in list_value(wrapper["errors"], "mail_handoff_errors"):
        require(
            isinstance(error, dict) and set(error) == {"message_id", "kind"}, "mail_handoff_error"
        )
        mid = bounded(error["message_id"], 128)
        require(
            mid in pending
            and mid not in errors
            and isinstance(error["kind"], str)
            and error["kind"] in MAIL_READ_ERRORS,
            "mail_handoff_error",
        )
        errors[mid] = error["kind"]
    omitted = set(pending) - set(reads)
    partial = {mid for mid, full in reads.items() if not full["body_complete"]}
    require(
        set(errors) == omitted | partial
        and all(errors[mid] == "body_incomplete" for mid in partial)
        and all(errors[mid] != "body_incomplete" for mid in omitted),
        "mail_handoff_coverage",
    )
    require(
        type(wrapper["body_reads_omitted"]) is int
        and wrapper["body_reads_omitted"] == len(omitted)
        and type(wrapper["complete"]) is bool
        and wrapper["complete"] == (search["complete"] and not omitted and not errors),
        "mail_handoff_coverage",
    )
    return search, reads


def mail(request):
    require(set(request) == {"schema_version", "source", "collections"}, "mail_capture_shape")
    collections = request["collections"]
    require(isinstance(collections, list) and 1 <= len(collections) <= 20, "mail_capture_bounds")
    observations, scopes, seen, count = [], [], {}, 0
    native_threads = {}
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
        body_reads = {}
        if request["schema_version"] == 2:
            evidence, body_reads = mail_handoff(evidence)
        mail_search(evidence)
        threads = evidence["threads"]
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
            # Latest IDs describe native identities even when their messages
            # are omitted from the retained body list.
            for native_id in [
                thread["thread_latest_message_id"],
                *(message["messageId"] for message in thread["messages"]),
            ]:
                if native_id is None:
                    continue
                require(
                    native_id not in native_threads
                    or native_threads[native_id] == thread["thread_id"],
                    "mail_identity_conflict",
                )
                native_threads[native_id] = thread["thread_id"]
            if not thread["complete"] or thread.get("messages_omitted"):
                gaps.append("thread incomplete or messages omitted")
            for message in thread["messages"]:
                count += 1
                require(count <= MAX_MESSAGES and isinstance(message, dict), "mail_message_bounds")
                identifier = bounded(message.get("messageId"), 128)
                require(MAIL_ID.fullmatch(identifier), "mail_message_identity")
                subject = bounded(message.get("subject"), 2000)
                source_at = message["internalDate"]
                if source_at is None:
                    gaps.append("message date unavailable; no source ordering proof")
                require(type(message.get("body_complete")) is bool, "mail_body_coverage")
                raw_body = bounded(message.get("body"), MAX_BODY)
                body_complete = message["body_complete"]
                if not body_complete:
                    gaps.append("message body incomplete")
                if identifier in body_reads:
                    full = body_reads[identifier]
                    raw_body, body_complete = full["body"], full["body_complete"]
                fingerprint = sha([subject, source_at, raw_body, body_complete])
                fields = (
                    {} if len(subject) > 300 else mail_fields(family, subject, mail_text(raw_body))
                )
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
                        "complete": body_complete,
                    }
                else:
                    require(prior["fingerprint"] == fingerprint, "mail_identity_conflict")
                    for field, value in fields.items():
                        require(
                            field not in prior["fields"] or prior["fields"][field] == value,
                            "mail_template_conflict",
                        )
                        prior["fields"][field] = value
                    if (clock, observed_at) > (prior["clock"], prior["observed_at"]):
                        prior.update(clock=clock, observed_at=observed_at)
        scopes.append(scope_record(f"mail:{family}", scope, capture["observed_at"], gaps))
    for identifier, item in sorted(seen.items()):
        fact = new_fact(
            "mail",
            "pending",
            item["observed_at"],
            source_at=item["source_at"],
            complete=item["complete"],
            **item["fields"],
        )
        fact["citation"] = {"kind": "mail", "id": identifier, "url": None}
        fact["evidence_id"] = mail_id(item["fingerprint"], fact)
        observations.append({"identity": None, "fact": fact})
    return observations, scopes


def prepare(request):
    require(
        isinstance(request, dict)
        and type(request.get("schema_version")) is int
        and (
            request["schema_version"] == 1
            or request["schema_version"] == 2
            and request.get("source") == "mail"
        ),
        "source_input_version",
    )
    if request.get("source") == "sessionize":
        return sessionize(request)
    if request.get("source") == "mail":
        return mail(request)
    raise SourceError("source_kind; use sessionize or mail and the documented capture owner")
