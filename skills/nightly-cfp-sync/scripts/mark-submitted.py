#!/usr/bin/env python3
"""Mark CFPs from submission confirmation mail: status sent, mail archived.

Reads the last 30 days of confirmations through the OneCLI gateway, which holds
the Google credential. A matched CFP gets `status: sent`, `user_actioned: true`
and its talk titles through check-cfps' commit-state.py. Then the mail leaves the
inbox. Mail with no matching CFP stays in the inbox and is reported.

No arguments. Exit 0 prints {"marked": [slugs], "archived": N, "unmatched": [senders]}.
Exit 1: Gmail or state failure, with the cause on stderr; nothing further runs.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
STATE = Path("/workspace/group/cfp-state.json")
INSTALLED_COMMIT = Path("/home/node/.claude/skills/tessl__check-cfps/scripts/commit-state.py")
SOURCE_COMMIT = Path(__file__).resolve().parents[2] / "check-cfps" / "scripts" / "commit-state.py"
QUERIES = (
    'from:events.sessionize.com "successfully submitted"',
    'from:socallinuxexpo.org subject:"session proposal has been received"',
    'from:oxfordabstracts.com subject:"thank you for submitting your proposal"',
)
SESSIONIZE = re.compile(r"([a-z0-9-]+)@events\.sessionize\.com", re.IGNORECASE)
RECEIVED_PREFIX = "your session proposal has been received:"


def gmail(method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> dict:
    query = f"?{urllib.parse.urlencode(params, doseq=True)}" if params else ""
    request = urllib.request.Request(
        f"{GMAIL}{path}{query}",
        data=None if body is None else json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method=method,
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def confirmations(api: Callable[..., dict]) -> list[dict]:
    found = {}
    for query in QUERIES:
        listed = api("GET", "/messages", params={"q": f"{query} newer_than:30d", "maxResults": 100})
        for row in listed.get("messages", []):
            if row["id"] in found:
                continue
            raw = api(
                "GET",
                f"/messages/{row['id']}",
                params={"format": "metadata", "metadataHeaders": ["From", "Subject"]},
            )
            headers = {h["name"].lower(): h["value"] for h in raw["payload"]["headers"]}
            found[row["id"]] = {
                "id": row["id"],
                "from": headers.get("from", ""),
                "subject": headers.get("subject", ""),
                "inbox": "INBOX" in raw.get("labelIds", []),
            }
    return list(found.values())


def _latest(records: dict, matches: Callable[[str, dict], bool]) -> str | None:
    hits = [slug for slug, record in records.items() if matches(slug, record)]
    return max(hits, key=lambda slug: str(records[slug].get("conf_date") or ""), default=None)


def _new_sessionize_record(slug: str) -> dict:
    return {
        "name": slug.replace("-", " ").title(),
        "slug": slug,
        "cfp_url": f"https://sessionize.com/{slug}",
        "source": "submission-mail",
    }


def resolve(mail: dict, records: dict) -> tuple[str | None, str | None, dict | None]:
    """The (slug, talk title, record to create) a confirmation belongs to."""
    sender, subject = mail["from"], mail["subject"]
    sessionize = SESSIONIZE.search(sender)
    if sessionize:
        slug = sessionize.group(1).lower()
        known = _latest(
            records,
            lambda key, record: (
                key == slug or str(record.get("cfp_url") or "").rstrip("/").endswith(f"/{slug}")
            ),
        )
        return known or slug, subject, None if known else _new_sessionize_record(slug)
    if "socallinuxexpo.org" in sender:
        title = subject[len(RECEIVED_PREFIX) :].strip() if ":" in subject else None
        known = _latest(
            records, lambda _, record: "socallinuxexpo.org" in str(record.get("cfp_url") or "")
        )
        return known, title, None
    if "oxfordabstracts.com" in sender:
        known = _latest(
            records,
            lambda key, record: "ocx" in key.lower() or "ocx" in str(record.get("name")).lower(),
        )
        return known, None, None
    return None, None, None


def plan(mails: list[dict], records: dict, today: str) -> tuple[dict, list[dict], list[str]]:
    """The tracker updates, the mail to archive and the senders nothing matched."""
    updates: dict = {}
    archive: list[dict] = []
    unmatched: list[str] = []
    for mail in mails:
        slug, title, created = resolve(mail, records)
        if slug is None:
            if mail["inbox"]:
                unmatched.append(mail["from"])
            continue
        if mail["inbox"]:
            archive.append(mail)
        record = updates.get(slug) or created or records[slug]
        if record.get("status") == "sent" and slug not in updates:
            continue
        talks = set(record.get("submitted_talks", []))
        if title:
            talks.add(title)
        updates[slug] = {
            **record,
            "status": "sent",
            "user_actioned": True,
            "updated": today,
            "submitted_talks": sorted(talks),
        }
    return updates, archive, sorted(set(unmatched))


def commit(updates: dict) -> None:
    script = INSTALLED_COMMIT if INSTALLED_COMMIT.exists() else SOURCE_COMMIT
    done = subprocess.run(
        [sys.executable, str(script), "--state", str(STATE), "--submission-confirmed"],
        input=json.dumps(updates),
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise RuntimeError(f"commit-state failed: {done.stderr.strip()}")


def run(api: Callable[..., dict] = gmail, today: str | None = None) -> dict:
    day = today or datetime.now(UTC).date().isoformat()
    records = {
        slug: record
        for slug, record in json.loads(STATE.read_text()).items()
        if not slug.startswith("_") and isinstance(record, dict)
    }
    updates, archive, unmatched = plan(confirmations(api), records, day)
    if updates:
        commit(updates)
    for mail in archive:
        api("POST", f"/messages/{mail['id']}/modify", body={"removeLabelIds": ["INBOX"]})
    return {"marked": sorted(updates), "archived": len(archive), "unmatched": unmatched}


def main() -> int:
    try:
        print(json.dumps(run()))
    except (OSError, RuntimeError, ValueError, KeyError, urllib.error.URLError) as error:
        print(f"mark-submitted: {type(error).__name__}: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
