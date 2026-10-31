#!/usr/bin/env python3
"""CFP submissions ledger owner and bounded source-import CLI. No network calls.

One JSON stdin object per action; stdout is JSON, diagnostics go to stderr.
record: {identity: {event: {namespace,id}, talk: {namespace,id}} | null,
         fact: <record1>, coverage?: <coverage1>}.
manual: same input, restricted to an owner-declared fact and owner citation.
query: {start: ISO date, end: ISO date} (inclusive event-date overlap).
history: same range, also returns known inactive submission history.
bind: {identity: <identity>, evidence_id, owner_binding: <binding1>}.
import: source1 Sessionize rendered-tree or maintained mail packet; all
        observations/coverage validate before one ledger commit. Result has
        recorded/changed/observations/identities_resolved/ledger_sha256.
reconcile: {identity: <identity>, slug: <existing exact CFP slug>}.
sync: {}. Derived exact-reference CFP pass; reports each outcome, checked and
      final ledger hashes, generation_changed and complete (not collection
      completeness), including absent CFP state. The complete supported result
      budget is admitted before any guarded CFP owner invocation. A batch that
      cannot fit holds without effects; use per-identity reconcile instead.
      Both derived actions use the existing guarded CFP owner.
record/manual return recorded/changed/identity_resolved/ledger_sha256;
bind returns bound/changed/ledger_sha256; query/history return matches,
unresolved, coverage and an initialized ledger's canonical hash.
The schema reference defines all fields. Inputs describe collected evidence;
this owner does not authenticate a DOM, mail, source timestamp or owner message.
Source1 inputs, fact1/binding1 fields and coverage are documented beside this
owner. Unknown templates/identities remain unresolved; future/malformed state,
unsafe files, conflicts, unsupported captures and capacity failures HOLD.

Exit 0 emits a result, including preserved reconciliation conflicts; exit 1
holds retained state and names recovery. Unexpected implementation errors
propagate. Only an absent ledger is initialized by record/manual/import.
query/history/bind/reconcile/sync never initialize it. The scripts send
nothing, create no provider objects and never advance heartbeat/cadence state.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urlsplit

from submission_sources import SourceError, adapter_kind, material

LEDGER = Path("/workspace/group/cfp-submissions.json")
CFP_STATE = Path("/workspace/group/cfp-state.json")
OWNER = Path("/home/node/.claude/skills/tessl__check-cfps/scripts/commit-state.py")
MAX_BYTES = 16 * 1024 * 1024
MAX_INPUT = 2 * 1024 * 1024
MAX_OUTPUT = 256 * 1024
# Extra serialized bytes beyond identity for the finite single-slug owner result
# or fixed controlled recovery. Unsupported larger outcomes are programmer faults,
# never a query-capacity/no-effect hold after a guarded write.
MAX_SYNC_OUTCOME = 4096
MAX_ROWS = 5000
MAX_IMPORT = 5000
MAX_FACTS = 100
STATUSES = {"submitted", "in_evaluation", "accepted", "declined", "waitlisted", "withdrawn"}
ACTIVE = {"submitted", "in_evaluation", "accepted", "waitlisted"}
KINDS = {"sessionize", "mail", "owner", "event_official"}
CFP_STATUSES = {"open", "approved", "dismissed", "conflict", "sent", "remind", "expired"}


class Hold(ValueError):
    """Supported, actionable no-effect refusal."""


def obj(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise Hold("invalid_shape; supply the documented complete field set")
    return value


def version(value):
    if not isinstance(value, dict) or type(value.get("schema_version")) is not int:
        raise Hold("damaged_schema; restore a supported backup before recording")
    if value["schema_version"] != 1:
        raise Hold("unsupported_version; update the owner, preserve the retained file")


def text(value, maximum=300):
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= maximum
        or not value.strip()
        or any(ord(c) < 32 for c in value)
    ):
        raise Hold("invalid_text; supply bounded visible text without control characters")
    return value


def instant(value):
    text(value, 40)
    if "T" not in value:
        raise Hold("invalid_time; use a zoned ISO date-time, not a local calendar date")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise Hold("invalid_time; supply a zoned ISO source/observation instant") from error
    if parsed.tzinfo is None:
        raise Hold("invalid_time; supply an explicit timezone offset")
    return parsed


def day(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise Hold("invalid_date; supply an ISO calendar date")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise Hold("invalid_date; supply a real ISO calendar date") from error


def url(value):
    text(value, 2000)
    try:
        p = urlsplit(value)
        good = (
            p.scheme in {"http", "https"}
            and p.hostname
            and not p.username
            and not p.password
            and p.port in {None, 80, 443}
            and not p.query
            and not p.fragment
        )
    except ValueError as error:
        raise Hold("invalid_citation; supply a public nonsecret canonical URL") from error
    if not good:
        raise Hold("invalid_citation; omit credentials, query tokens and fragments")
    return value.rstrip("/")


def identity(value):
    obj(value, {"event", "talk"})
    for component in value.values():
        obj(component, {"namespace", "id"})
        text(component["namespace"], 60)
        text(component["id"], 300)
    return value


def key(value):
    identity(value)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def encode(value):
    try:
        result = json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()
    except (ValueError, UnicodeEncodeError) as error:
        raise Hold("invalid_encoding; supply finite UTF-8 evidence") from error
    if len(result) > MAX_BYTES:
        raise Hold("ledger_capacity; retain the file and inspect source bounds before recording")
    return result


def digest(value):
    return hashlib.sha256(encode(value)).hexdigest()


def fact(value):
    version(value)
    obj(
        value,
        {
            "schema_version",
            "evidence_id",
            "kind",
            "citation",
            "observed_at",
            "source_at",
            "complete",
            "event_name",
            "talk_title",
            "status",
            "submitted_on",
            "dates",
            "city",
            "co_speakers",
            "cfp_url",
        },
    )
    text(value["evidence_id"], 500)
    if (
        not isinstance(value["kind"], str)
        or value["kind"] not in KINDS
        or type(value["complete"]) is not bool
    ):
        raise Hold("invalid_evidence; supply a supported kind and explicit completeness")
    obj(value["citation"], {"kind", "id", "url"})
    if value["citation"]["kind"] != value["kind"]:
        raise Hold("citation_binding; preserve the collected source kind")
    text(value["citation"]["id"], 500)
    if value["citation"]["url"] is not None:
        url(value["citation"]["url"])
    instant(value["observed_at"])
    if value["source_at"] is not None:
        if instant(value["source_at"]) > instant(value["observed_at"]):
            raise Hold("future_source; inspect the source timestamp instead of guessing")
    for field in ("event_name", "talk_title"):
        if value[field] is not None:
            text(value[field])
    if value["status"] is not None and (
        not isinstance(value["status"], str) or value["status"] not in STATUSES
    ):
        raise Hold("invalid_status; preserve an unknown status as null")
    if value["kind"] == "event_official" and any(
        value[name] is not None for name in ("status", "submitted_on", "co_speakers")
    ):
        raise Hold("event_scope; an event page alone is not the owner's submission evidence")
    if value["submitted_on"] is not None:
        day(value["submitted_on"])
    if value["dates"] is not None:
        obj(value["dates"], {"start", "end"})
        if day(value["dates"]["start"]) > day(value["dates"]["end"]):
            raise Hold("invalid_range; event end must not precede its start")
    if value["city"] is not None:
        text(value["city"])
    if value["co_speakers"] is not None:
        if not isinstance(value["co_speakers"], list) or len(value["co_speakers"]) > 50:
            raise Hold("invalid_speakers; supply a bounded explicit list or null")
        for speaker in value["co_speakers"]:
            text(speaker)
    if value["cfp_url"] is not None:
        url(value["cfp_url"])
    return value


def coverage(value):
    version(value)
    obj(value, {"schema_version", "id", "source", "scope", "checked_at", "complete", "gaps"})
    for name, maximum in (("id", 500), ("source", 100), ("scope", 2000)):
        text(value[name], maximum)
    instant(value["checked_at"])
    if type(value["complete"]) is not bool or not isinstance(value["gaps"], list):
        raise Hold("invalid_coverage; retain pagination and source gaps explicitly")
    if len(value["gaps"]) > 100 or (value["complete"] and value["gaps"]):
        raise Hold("invalid_coverage; a source with gaps cannot be complete")
    for gap in value["gaps"]:
        text(gap)
    return value


def validate(store):
    version(store)
    obj(store, {"schema_version", "rows", "unresolved", "coverage"})
    for name in ("rows", "unresolved", "coverage"):
        if not isinstance(store[name], dict) or len(store[name]) > MAX_ROWS:
            raise Hold("ledger_shape; restore a bounded supported ledger backup")
    seen = set()
    for identifier, row in store["rows"].items():
        version(row)
        obj(row, {"schema_version", "identity", "facts", "owner_bindings"})
        if identifier != key(row["identity"]):
            raise Hold("identity_binding; restore the retained event/talk identity")
        if not isinstance(row["facts"], dict) or not 1 <= len(row["facts"]) <= MAX_FACTS:
            raise Hold("evidence_capacity; inspect the retained row without deleting evidence")
        for evidence_id, value in row["facts"].items():
            fact(value)
            if evidence_id != value["evidence_id"] or evidence_id in seen:
                raise Hold("evidence_binding; one source observation cannot identify two rows")
            seen.add(evidence_id)
        if not isinstance(row["owner_bindings"], dict):
            raise Hold("binding_shape; retain explicit owner binding provenance")
        for evidence_id, binding in row["owner_bindings"].items():
            if evidence_id not in row["facts"]:
                raise Hold("binding_shape; owner bindings must retain their source observation")
            obj(binding, {"schema_version", "owner_message_id", "observed_at"})
            version(binding)
            text(binding["owner_message_id"], 500)
            instant(binding["observed_at"])
    for evidence_id, value in store["unresolved"].items():
        fact(value)
        if evidence_id != value["evidence_id"] or evidence_id in seen:
            raise Hold("evidence_binding; resolve the conflicting source identity explicitly")
        seen.add(evidence_id)
    for identifier, value in store["coverage"].items():
        coverage(value)
        if identifier != value["id"]:
            raise Hold("coverage_binding; preserve the actual source scope identity")
    encode(store)
    return store


def empty():
    return {"schema_version": 1, "rows": {}, "unresolved": {}, "coverage": {}}


def stamp(info):
    return (
        info.st_dev,
        info.st_ino,
        info.st_mtime_ns,
        info.st_ctime_ns,
        info.st_size,
        info.st_mode,
        info.st_uid,
        info.st_nlink,
    )


def file_bytes(path, private=True):
    """Read a private, named, single-link regular file through a held descriptor."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
        raise Hold("unsafe_file; restore a regular single-link owner file, not an alias")
    if private and (before.st_uid != os.geteuid() or stat.S_IMODE(before.st_mode) != 0o600):
        raise Hold("private_file; restore the owning user's private0600 file")
    if before.st_size > MAX_BYTES:
        raise Hold("ledger_capacity; retain oversized bytes and inspect the owner backup")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        current = os.fstat(handle.fileno())
        data = handle.read(MAX_BYTES + 1)
        after = path.lstat()
        if stamp(before) != stamp(current) or stamp(before) != stamp(after):
            raise Hold("changed_file; re-read the owner file after its writer finishes")
    if len(data) > MAX_BYTES:
        raise Hold("ledger_capacity; retain the owner file without truncation")
    return data


def read(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return None
    try:
        raw = file_bytes(path)
    except FileNotFoundError as error:
        raise Hold("changed_file; re-read the owner file after its writer finishes") from error
    try:
        result = decode(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Hold("damaged_ledger; restore a supported backup; never reset the file") from error
    return validate(result)


def decode(raw):
    def pairs(items):
        result = {}
        for name, value in items:
            if name in result:
                raise Hold("duplicate_field; retain ambiguous bytes and restore supported input")
            result[name] = value
        return result

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (Hold, UnicodeDecodeError):
        raise
    except (ValueError, RecursionError) as error:
        raise json.JSONDecodeError("invalid bounded JSON input", "", 0) from error


def result_text(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


@contextmanager
def locked(path):
    lock = path.with_name(path.name + ".lock")
    fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.geteuid():
            raise Hold("unsafe_lock; restore the single owner lock without unlinking a live lock")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise Hold(
                "ledger_busy; wait for the owning writer to finish before retrying"
            ) from error
        yield
    finally:
        os.close(fd)


def publish(path, store):
    data = encode(store)
    fd, name = tempfile.mkstemp(prefix=".cfp-submissions-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def merge_observation(store, request, manual=False):
    if not isinstance(request, dict) or set(request) not in (
        {"identity", "fact"},
        {"identity", "fact", "coverage"},
    ):
        raise Hold("invalid_input; supply identity, fact and optional coverage")
    observation = fact(request["fact"])
    if manual and observation["kind"] != "owner":
        raise Hold("manual_source; link the actual owner note, not a fabricated provider receipt")
    row_key = key(request["identity"]) if request["identity"] is not None else None
    scope = coverage(request["coverage"]) if "coverage" in request else None
    evidence_id = observation["evidence_id"]
    found = store["unresolved"].get(evidence_id)
    found_key = None
    found_identity = request["identity"]
    for identifier, row in store["rows"].items():
        if evidence_id in row["facts"]:
            found, found_key = row["facts"][evidence_id], identifier
            found_identity = row["identity"]
    refresh = (
        found is not None
        and material(found) == material(observation)
        and adapter_kind(found_identity, found) is not None
        and adapter_kind(found_identity, found) == adapter_kind(found_identity, observation)
    )
    if found is not None and refresh:
        observation = {
            **observation,
            "observed_at": max([found["observed_at"], observation["observed_at"]], key=instant),
        }
    if (
        (found == observation or refresh)
        and row_key is None
        and found_key is not None
        and evidence_id in store["rows"][found_key]["owner_bindings"]
    ):
        # A source that cannot identify the proposal must not undo or reject
        # an explicit retained owner binding when its unchanged receipt recurs.
        row_key = found_key
    if found is not None and ((found != observation and not refresh) or found_key != row_key):
        raise Hold("evidence_conflict; preserve the original and obtain an explicit binding")
    if row_key is None:
        store["unresolved"][evidence_id] = observation
    else:
        row = store["rows"].setdefault(
            row_key,
            {
                "schema_version": 1,
                "identity": request["identity"],
                "facts": {},
                "owner_bindings": {},
            },
        )
        row["facts"][evidence_id] = observation
    if scope is not None:
        prior = store["coverage"].get(scope["id"])
        if prior is not None:
            old, new = instant(prior["checked_at"]), instant(scope["checked_at"])
            if new < old or (new == old and prior != scope):
                raise Hold("coverage_conflict; retain the newer source scope and recollect")
        store["coverage"][scope["id"]] = scope
    return row_key is not None


def record_many(path, requests, scopes=(), manual=False):
    if not isinstance(requests, list) or len(requests) > MAX_IMPORT:
        raise Hold("input_capacity; split bounded observations without dropping evidence")
    with locked(path):
        original = read(path)
        store = json.loads(json.dumps(original)) if original is not None else empty()
        resolved = sum(merge_observation(store, request, manual) for request in requests)
        for scope in scopes:
            coverage(scope)
            prior = store["coverage"].get(scope["id"])
            if prior is not None:
                old, new = instant(prior["checked_at"]), instant(scope["checked_at"])
                if new < old or (new == old and prior != scope):
                    raise Hold("coverage_conflict; retain the newer source scope and recollect")
            store["coverage"][scope["id"]] = scope
        validate(store)
        if original != store:
            publish(path, store)
        return {
            "schema_version": 1,
            "recorded": True,
            "changed": original != store,
            "observations": len(requests),
            "identities_resolved": resolved,
            "ledger_sha256": digest(store),
        }


def record(path, request, manual=False):
    result = record_many(path, [request], manual=manual)
    result["identity_resolved"] = result.pop("identities_resolved") == 1
    result.pop("observations")
    return result


def bind(path, request):
    obj(request, {"evidence_id", "identity", "owner_binding"})
    row_key = key(request["identity"])
    evidence_id = text(request["evidence_id"], 500)
    binding = request["owner_binding"]
    version(binding)
    obj(binding, {"schema_version", "owner_message_id", "observed_at"})
    text(binding["owner_message_id"], 500)
    instant(binding["observed_at"])
    with locked(path):
        store = read(path)
        if store is None or evidence_id not in store["unresolved"]:
            # Idempotent only for this exact prior binding; never rebind another row.
            row = store["rows"].get(row_key) if store else None
            if row and row["owner_bindings"].get(evidence_id) == binding:
                return {
                    "schema_version": 1,
                    "bound": True,
                    "changed": False,
                    "ledger_sha256": digest(store),
                }
            raise Hold("binding_missing; identify the retained unresolved observation")
        observation = store["unresolved"].pop(evidence_id)
        row = store["rows"].setdefault(
            row_key,
            {
                "schema_version": 1,
                "identity": request["identity"],
                "facts": {},
                "owner_bindings": {},
            },
        )
        row["facts"][evidence_id] = observation
        row["owner_bindings"][evidence_id] = binding
        validate(store)
        publish(path, store)
        return {"schema_version": 1, "bound": True, "changed": True, "ledger_sha256": digest(store)}


def resolve(row, field):
    """Resolve comparable source facts; never rank a missing source clock as latest."""
    facts = list(row["facts"].values())
    native = [v for v in facts if adapter_kind(row["identity"], v) == "sessionize"]
    if native:
        latest = max(instant(v["observed_at"]) for v in native)
        native_ids = {v["evidence_id"] for v in native}
        facts = [v for v in facts if v["evidence_id"] not in native_ids] + [
            v for v in native if instant(v["observed_at"]) == latest
        ]
    candidates = [v for v in facts if v["complete"] and v[field] is not None]
    if field in {"dates", "city"}:
        # Mail/manual claims do not replace the event's own date/city source.
        candidates = [v for v in candidates if v["kind"] in {"event_official", "sessionize"}]
        if any(v["kind"] == "event_official" for v in candidates):
            candidates = [v for v in candidates if v["kind"] == "event_official"]
    if not candidates:
        return None, "unknown", []
    values = {json.dumps(v[field], sort_keys=True, ensure_ascii=False) for v in candidates}
    if len(values) > 1:
        if any(v["source_at"] is None for v in candidates):
            return None, "conflict", [provenance(v) for v in candidates]
        latest = max(instant(v["source_at"]) for v in candidates)
        candidates = [v for v in candidates if instant(v["source_at"]) == latest]
        if len({json.dumps(v[field], sort_keys=True) for v in candidates}) > 1:
            return None, "conflict", [provenance(v) for v in candidates]
    return candidates[0][field], "known", [provenance(v) for v in candidates]


def provenance(value):
    return {name: value[name] for name in ("evidence_id", "citation", "observed_at", "source_at")}


def project(row):
    fields = (
        "event_name",
        "talk_title",
        "status",
        "submitted_on",
        "dates",
        "city",
        "co_speakers",
        "cfp_url",
    )
    result = {"identity": row["identity"], "fields": {}, "field_state": {}, "citations": {}}
    for name in fields:
        value, state, citations = resolve(row, name)
        result["fields"][name] = value
        result["field_state"][name] = state
        result["citations"][name] = citations
    result["checked_at"] = max((v["observed_at"] for v in row["facts"].values()), key=instant)
    result["owner_bindings"] = row["owner_bindings"]
    return result


def query(path, request, history=False):
    obj(request, {"start", "end"})
    start, end = day(request["start"]), day(request["end"])
    if start > end:
        raise Hold("invalid_range; query end must not precede start")
    store = read(path)
    result = {
        "schema_version": 1,
        "matches": [],
        "unresolved": [],
        "coverage": {
            "complete": False,
            "sources": [],
            "reason": "Last-seen evidence is not exhaustive submission history.",
        },
    }
    if store is None:
        result["coverage"]["reason"] = "Ledger uninitialized; collect actual source evidence."
        return result
    for _, row in sorted(store["rows"].items()):
        item = project(row)
        status, dates = item["fields"]["status"], item["fields"]["dates"]
        if status is not None and not history and status not in ACTIVE:
            continue
        if status is None or dates is None:
            result["unresolved"].append(item)
        elif day(dates["start"]) <= end and day(dates["end"]) >= start:
            result["matches"].append(item)
    result["unresolved"].extend(
        {"identity": None, "fact": value, "reason": "unbound event/talk identity"}
        for _, value in sorted(store["unresolved"].items())
    )
    result["coverage"]["sources"] = [v for _, v in sorted(store["coverage"].items())]
    result["ledger_sha256"] = digest(store)
    return result


def cfp_owner():
    if OWNER.exists():
        return OWNER
    # Fixed co-shipped source layout only, for repository execution/tests.
    source = Path(__file__).resolve().parents[2] / "check-cfps/scripts/commit-state.py"
    manifest = source.parents[3] / ".tessl-plugin/plugin.json"
    if source.is_file() and manifest.is_file():
        try:
            declaration = decode(manifest.read_bytes())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise Hold("cfp_owner_unavailable; restore the co-shipped plugin manifest") from error
        if (
            isinstance(declaration, dict)
            and declaration.get("name") == "jbaruch/nanoclaw-conferences"
            and isinstance(declaration.get("skills"), list)
            and "skills/check-cfps" in declaration["skills"]
            and "skills/cfp-submissions" in declaration["skills"]
        ):
            return source
    raise Hold("cfp_owner_unavailable; reinstall the complete Conferences plugin")


def owner_call(owner, arguments, value):
    completed = subprocess.run(
        [sys.executable, str(owner), *arguments],
        input=json.dumps(value),
        capture_output=True,
        text=True,
        timeout=45,
    )
    if completed.returncode:
        raise Hold("cfp_owner_hold; retain the ledger; inspect the supported CFP owner backup")
    try:
        result = decode(completed.stdout)
    except json.JSONDecodeError as error:
        raise Hold("cfp_owner_result; inspect owner installation before reconciliation") from error
    if not isinstance(result, dict):
        raise Hold("cfp_owner_result; update the complete owner before reconciliation")
    return result


def reconcile(path, cfp_path, request, expected_ledger_sha256=None):
    obj(request, {"identity", "slug"})
    text(request["slug"], 300)
    store = read(path)
    if expected_ledger_sha256 is not None and digest(store) != expected_ledger_sha256:
        raise Hold(
            "ledger_changed; restart the derived pass against its current checked generation"
        )
    row = store["rows"].get(key(request["identity"])) if store else None
    if row is None:
        raise Hold("identity_unknown; record an evidenced event/talk before reconciliation")
    item = project(row)
    if item["fields"]["status"] not in ACTIVE or item["fields"]["cfp_url"] is None:
        raise Hold("submission_unknown; resolve the status and exact CFP reference first")
    # The existing CFP file's mode is intentionally not changed by this reader.
    before = file_bytes(cfp_path, private=False)
    try:
        state = decode(before)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Hold(
            "cfp_damaged; restore the existing owner's backup without resetting it"
        ) from error
    if not isinstance(state, dict):
        raise Hold("cfp_damaged; restore the existing owner object")
    prior = state.get(request["slug"])
    if not isinstance(prior, dict):
        raise Hold("cfp_missing; identify an existing exact CFP record before reconciliation")
    version(prior)
    if not isinstance(prior.get("status"), str) or prior["status"] not in CFP_STATUSES:
        raise Hold("cfp_status; restore the current CFP owner's supported record")
    if prior.get("cfp_url") is None or url(prior["cfp_url"]) != url(item["fields"]["cfp_url"]):
        raise Hold("cfp_identity; select the exact source URL; titles do not establish identity")
    reference = url(item["fields"]["cfp_url"])
    matches = {
        slug
        for slug, candidate in state.items()
        if not slug.startswith("_")
        and isinstance(candidate, dict)
        and isinstance(candidate.get("cfp_url"), str)
        and candidate["cfp_url"].rstrip("/") == reference
    }
    if matches != {request["slug"]}:
        raise Hold("cfp_identity; resolve duplicate public references through the existing owner")
    if prior["status"] == "sent":
        return {"schema_version": 1, "outcome": "already_sent", "ledger_sha256": digest(store)}
    if prior.get("user_actioned") is True:
        return {
            "schema_version": 1,
            "outcome": "user_actioned_conflict",
            "recovery": "Preserved; resolve through the existing explicit owner workflow.",
        }
    proposed = {**prior, "status": "sent"}
    owner = cfp_owner()
    fingerprint = owner_call(owner, ["--snapshot"], {request["slug"]: prior})
    with tempfile.TemporaryDirectory(prefix="cfp-submission-reconcile-") as temporary:
        directory = os.open(temporary, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchmod(directory, 0o700)
        finally:
            os.close(directory)
        expected = Path(temporary) / "expected.json"
        fd = os.open(expected, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(fingerprint, handle)
        # No ledger lock is held across the existing CFP owner's lock.
        if read(path) != store:
            raise Hold("ledger_changed; re-read its exact current evidence before reconciliation")
        response = owner_call(
            owner,
            [
                "--state",
                str(cfp_path),
                "--expected-digests",
                str(expected),
                "--unique-cfp-url",
                url(item["fields"]["cfp_url"]),
            ],
            {request["slug"]: proposed},
        )
    expected_keys = {"written", "skipped_user_actioned", "total_records", "skipped_changed"}
    if (
        set(response) != expected_keys
        or any(type(response[name]) is not int for name in expected_keys - {"skipped_changed"})
        or not 0 <= response["written"] <= 1
        or not 0 <= response["skipped_user_actioned"] <= 1
        or not 0 <= response["total_records"] <= sys.maxsize
        or response["skipped_changed"] not in ([], [request["slug"]])
    ):
        raise Hold("cfp_owner_result; inspect the existing owner result; do not replay blindly")
    outcome = "sent" if response["written"] == 1 else "conflict"
    return {
        "schema_version": 1,
        "outcome": outcome,
        "owner_result": response,
        "ledger_sha256": digest(store),
    }


def sync(path, cfp_path):
    """Project only uniquely matching exact CFP references through their owner.

    This derived pass is deliberately separate from the ledger transaction.
    Source import never creates CFP records or guesses a match from names.
    """
    store = read(path)
    if store is None:
        raise Hold("identity_unknown; record actual source evidence before synchronization")
    checked_hash = digest(store)
    try:
        state = decode(file_bytes(cfp_path, private=False))
    except FileNotFoundError:
        current = read(path)
        current_hash = digest(current) if current is not None else None
        return {
            "schema_version": 1,
            "complete": False,
            "results": [],
            "reason": "CFP state absent; initialize through its existing owner",
            "ledger_sha256": checked_hash,
            "current_ledger_sha256": current_hash,
            "generation_changed": current_hash != checked_hash,
            "unusable_cfp_references": 0,
            "reference_holds": [],
        }
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise Hold(
            "cfp_damaged; restore the existing owner's supported UTF-8 JSON backup"
        ) from error
    if not isinstance(state, dict):
        raise Hold("cfp_damaged; restore the existing owner object")
    references, reference_holds = {}, []
    for slug, prior in state.items():
        if slug.startswith("_") or not isinstance(prior, dict) or prior.get("cfp_url") is None:
            continue
        # Unknown retained CFP versions are still checked by reconcile before effects.
        try:
            reference = url(prior["cfp_url"])
        except Hold:
            reference_holds.append(
                {
                    "record_key_sha256": hashlib.sha256(
                        slug.encode("utf-8", errors="backslashreplace")
                    ).hexdigest(),
                    "reason": "unusable_reference",
                }
            )
            continue
        references.setdefault(reference, []).append(slug)
    candidates = []
    unresolved = bool(store["unresolved"])
    for _, row in sorted(store["rows"].items()):
        item = project(row)
        if item["fields"]["status"] is None:
            unresolved = True
        if item["fields"]["status"] not in ACTIVE:
            continue
        reference = item["fields"]["cfp_url"]
        slugs = references.get(url(reference), []) if reference is not None else []
        candidates.append((row, slugs))
    reserved = len(
        result_text(
            {
                "schema_version": 1,
                "complete": False,
                "results": [],
                "ledger_sha256": checked_hash,
                "current_ledger_sha256": checked_hash,
                "generation_changed": False,
                "unusable_cfp_references": len(reference_holds),
                "reference_holds": reference_holds,
            }
        ).encode()
    )
    for row, slugs in candidates:
        reserved += 2 + (
            len(result_text({"identity": row["identity"]}).encode()) + MAX_SYNC_OUTCOME
            if len(slugs) == 1
            else len(
                result_text(
                    {"identity": row["identity"], "outcome": "unmatched_reference"}
                ).encode()
            )
        )
    if reserved > MAX_OUTPUT:
        raise Hold(
            "sync_capacity; no CFP owner invoked; use per-identity reconcile and inspect "
            "each result without dropping retained evidence"
        )
    results = []
    for row, slugs in candidates:
        if len(slugs) != 1:
            results.append({"identity": row["identity"], "outcome": "unmatched_reference"})
            continue
        try:
            outcome = reconcile(
                path,
                cfp_path,
                {"identity": row["identity"], "slug": slugs[0]},
                expected_ledger_sha256=checked_hash,
            )
        except Hold as error:
            outcome = {"outcome": "hold", "recovery": str(error)}
        if len(result_text(outcome).encode()) > MAX_SYNC_OUTCOME:
            raise RuntimeError(
                "unexpected_sync_outcome_capacity; inspect actual CFP results before any retry"
            )
        results.append({"identity": row["identity"], **outcome})
    current = read(path)
    current_hash = digest(current) if current is not None else None
    return {
        "schema_version": 1,
        "complete": current_hash == checked_hash
        and not unresolved
        and all(v["outcome"] in {"sent", "already_sent"} for v in results),
        "results": results,
        "ledger_sha256": checked_hash,
        "current_ledger_sha256": current_hash,
        "generation_changed": current_hash != checked_hash,
        "unusable_cfp_references": len(reference_holds),
        "reference_holds": reference_holds,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=["record", "manual", "bind", "query", "history", "reconcile", "sync", "import"],
    )
    args = parser.parse_args(argv)
    try:
        raw = (
            sys.stdin.buffer.read(MAX_INPUT + 1)
            if hasattr(sys.stdin, "buffer")
            else sys.stdin.read(MAX_INPUT + 1)
        )
        if len(raw) > MAX_INPUT:
            raise Hold("input_capacity; split the actual observations without dropping evidence")
        try:
            request = decode(raw)
        except UnicodeDecodeError:
            raise Hold("invalid_input_utf8; encode the documented JSON input as UTF-8") from None
        except json.JSONDecodeError:
            raise Hold(
                "invalid_input_json; repair the documented JSON input before retrying"
            ) from None
        if args.action == "import":
            from submission_sources import prepare

            observations, scopes = prepare(request)
            result = record_many(LEDGER, observations, scopes)
        elif args.action in {"record", "manual"}:
            result = record(LEDGER, request, manual=args.action == "manual")
        elif args.action in {"query", "history"}:
            result = query(LEDGER, request, history=args.action == "history")
        elif args.action == "sync":
            obj(request, set())
            result = sync(LEDGER, CFP_STATE)
        elif args.action == "bind":
            result = bind(LEDGER, request)
        else:
            result = reconcile(LEDGER, CFP_STATE, request)
        output = result_text(result)
        if len(output.encode()) > MAX_OUTPUT:
            raise Hold(
                "query_capacity; narrow the query or inspect unresolved source evidence; "
                "no absence is established"
            )
    except (Hold, SourceError) as error:
        print(f"cfp-submissions: {error}", file=sys.stderr)
        return 1
    except OSError:
        print(
            "cfp-submissions: source_io; preserve files and restore owner access/installation",
            file=sys.stderr,
        )
        return 1
    except subprocess.TimeoutExpired:
        print(
            "cfp-submissions: cfp_owner_timeout; inspect the actual CFP result before retrying",
            file=sys.stderr,
        )
        return 1
    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
