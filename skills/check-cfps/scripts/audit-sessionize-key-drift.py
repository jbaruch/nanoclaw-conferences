#!/usr/bin/env python3
"""Audit cfp-state.json for sessionize-sourced entries whose dict key
has drifted from the canonical URL slug.

Step 4's Sessionize branch derives the slug from `cfp_url`'s first path
segment, but the dict key was historically used as the slug. Drift
patterns observed in the 2026-05-03 audit (closes
`jbaruch/nanoclaw-admin#192`):

  - versioning suffix added to key (`agentcon-miami` -> `agentcon-miami-2026`)
  - aliasing (`pth-2026` -> `porto-tech-hub-2026`)
  - regional disambiguation (`infobip-shift-2026` -> `infobip-shift-zadar-2026`)
  - abbreviation -> fullname (`wpninja-ug-ch` ->
    `workplace-security-ninja-user-group-switzerland-2603-2026`)

Report-only. Does NOT mutate state — rewriting the dict key changes the
primary identity of the entry, which downstream code may reference; key
renormalization is a separate operator decision.

Usage:
  python3 audit-sessionize-key-drift.py [--state-path /path/to/cfp-state.json]

Output (stdout, JSON last line):
  {
    "checked":    <int>,            # number of sessionize-sourced entries inspected
    "drifted":    [...],            # list of {key, url_slug, cfp_url}
    "missing_cfp_url": [...]        # list of keys with no cfp_url to derive a slug from
  }

Exit code 0 on success (including state-file-not-found, which is a no-op),
non-zero on read failure (with diagnostic on stderr).
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import urlparse

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")

SESSIONIZE_SOURCE = "sessionize-speaker-api"


def derive_slug(cfp_url: str) -> str | None:
    """Return the first path segment of cfp_url, or None if unparseable.

    Sessionize URLs follow `https://sessionize.com/<slug>` (and subdomain
    variants); the slug is the first path segment. An empty path or a
    URL with no path returns None — the audit reports those separately
    in `missing_cfp_url`.

    Scheme-less inputs (e.g. `sessionize.com/foo`) also return None
    rather than treating the host as the slug. `urlparse('sessionize.com/foo')`
    puts the whole string in `.path` with empty `.scheme` and `.netloc`,
    which would otherwise produce `sessionize.com` as a fake "slug" and
    a misleading drift entry. Requiring both scheme and netloc keeps
    the audit honest about what's actually a Sessionize URL.
    """
    if not cfp_url or not isinstance(cfp_url, str):
        return None
    try:
        parsed = urlparse(cfp_url)
    except ValueError:
        return None
    if not parsed.scheme or not parsed.netloc:
        return None
    path = (parsed.path or "").strip("/")
    if not path:
        return None
    return path.split("/")[0]


def audit(state: dict) -> tuple[int, list[dict], list[str]]:
    """Walk sessionize-sourced entries. Return (checked, drifted, missing_cfp_url)."""
    checked = 0
    drifted: list[dict] = []
    missing_cfp_url: list[str] = []
    for key, entry in state.items():
        if key.startswith("_"):
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("source") != SESSIONIZE_SOURCE:
            continue
        checked += 1
        cfp_url = entry.get("cfp_url", "")
        url_slug = derive_slug(cfp_url)
        if url_slug is None:
            missing_cfp_url.append(key)
            continue
        if url_slug != key:
            drifted.append({"key": key, "url_slug": url_slug, "cfp_url": cfp_url})
    return checked, drifted, missing_cfp_url


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Audit cfp-state.json for sessionize key/url-slug drift (report-only)."
    )
    parser.add_argument(
        "--state-path",
        type=Path,
        default=DEFAULT_STATE_PATH,
        help=f"Path to cfp-state.json (default: {DEFAULT_STATE_PATH})",
    )
    args = parser.parse_args(argv)

    if not args.state_path.exists():
        sys.stderr.write(
            f"audit-sessionize-key-drift: state file not found at {args.state_path} — "
            f"nothing to audit\n"
        )
        print(json.dumps({"checked": 0, "drifted": [], "missing_cfp_url": []}))
        return 0

    try:
        state = json.loads(args.state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        sys.stderr.write(
            f"audit-sessionize-key-drift: failed to read {args.state_path}: "
            f"{type(exc).__name__}: {exc}\n"
        )
        return 1

    if not isinstance(state, dict):
        sys.stderr.write(
            f"audit-sessionize-key-drift: {args.state_path} root is "
            f"{type(state).__name__}, expected dict; aborting\n"
        )
        return 1

    checked, drifted, missing_cfp_url = audit(state)

    print(
        json.dumps(
            {
                "checked": checked,
                "drifted": drifted,
                "missing_cfp_url": missing_cfp_url,
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
