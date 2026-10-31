#!/usr/bin/env python3
"""Lock-owning committer for the check-cfps Step 8 state write.

Step 8 used to have the agent write cfp-state.json directly, which left
the pipeline's main writer outside the advisory-lock discipline the
maintenance scripts follow (state_lock.py) — exactly the lost-update
window the lock exists to close (jbaruch/nanoclaw-conferences#35). This
script is the single committer for the run's working set: the agent
never writes the state file itself.

Input (stdin): a JSON object mapping slug -> record dict — the in-memory
working set after Step 8's priority rules were applied. `_`-prefixed keys
are rejected: config keys (`_blocked_prefixes`) are user-managed and the
freshness heartbeat (`_last_checked`) has its own single writer
(stamp-last-checked.py).

Under the advisory lock it re-reads the on-disk state and applies the
working set as per-slug replacements, so a concurrent writer's updates to
OTHER slugs always survive. One invariant is re-checked at commit time
against the fresh read: a record that is `user_actioned: true` ON DISK is
never overwritten (the agent's copy may predate the user's action) —
those slugs are skipped and counted. Absent state file = first run =
empty state, and that empty state is materialized on disk under the same
lock even when the working set writes no record: the Step 8 stampers
that run next (stamp-schema-version.py, stamp-last-checked.py) exit 1 on
a missing file, so a first run whose discovery found nothing has to leave
a readable `{}` behind. An already-present state file with nothing to
write is left untouched.

Output (stdout, JSON): {"written": N, "skipped_user_actioned": M,
"total_records": T} — T is the record count in the file after commit.
Optional --expected-digests FILE adds source-snapshot guards. The JSON file
maps every working-set slug to record_digest(original_record), using None
for an absent record. Under the same write lock, changed source records are
skipped and reported in skipped_changed; operator actions still win. The
legacy output shape stays unchanged when this option is absent.
Optional --unique-cfp-url URL requires --expected-digests and exactly one
proposed slug whose cfp_url matches that public reference. Under the SAME
write lock, the current non-config records must contain exactly that one
slug with the reference (ignoring trailing slashes only); otherwise it is
skipped_changed, with no write. This is refusal-only, never a new writer or
permission. Unique-reference zero-write calls never initialize an absent
state file; existing callers retain their original bootstrap behavior.
--snapshot instead reads a slug -> original record (or null for absence)
JSON object from stdin and emits its fingerprint mapping without opening the
state file. It is mutually exclusive with --expected-digests. Complete guarded
writes with the owner's stamp-schema-version.py; snapshot mode has no writes.

Exit 0 on success; exit 1 with a stderr diagnostic on malformed stdin,
`_`-prefixed or non-dict payload entries, unreadable state, write
failure, or lock failure.
"""

import argparse
import hashlib
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path


def _load_state_lock():
    """Reuse the shared advisory-lock module from the sibling
    state_lock.py so the cfp-state write discipline has exactly one
    definition (same reuse pattern as backfill-name.py's
    `_load_dedup_helpers`)."""
    sibling = Path(__file__).with_name("state_lock.py")
    spec = importlib.util.spec_from_file_location("_cfps_state_lock", sibling)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load state_lock.py from {sibling}: the check-cfps script "
            "bundle looks incomplete — restore the sibling module next to "
            "commit-state.py (or reinstall the plugin) and retry"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


state_lock = _load_state_lock()

DEFAULT_STATE_PATH = Path("/workspace/group/cfp-state.json")


def _atomic_write_json(path, payload):
    """Write `payload` as JSON to `path` via temp file + fsync + os.replace,
    preserving the existing file's mode (0644 fallback). Raises on failure;
    cleanup uses try/finally (no broad except) per
    `coding-policy: error-handling`."""
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = 0o644
    tmp = tempfile.NamedTemporaryFile(
        mode="w",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
        encoding="utf-8",
    )
    replaced = False
    try:
        json.dump(payload, tmp, indent=2, ensure_ascii=False)
        tmp.flush()
        os.fsync(tmp.fileno())
        tmp.close()
        os.chmod(tmp.name, mode)
        os.replace(tmp.name, path)
        replaced = True
    finally:
        if not replaced:
            if not tmp.closed:
                tmp.close()
            try:
                os.unlink(tmp.name)
            except FileNotFoundError:
                pass


def record_digest(record: object) -> str:
    """Canonical source fingerprint shared by snapshot producers and this writer."""
    raw = json.dumps(
        record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Commit the Step 8 working set to cfp-state.json under the advisory lock."
    )
    parser.add_argument("--state", type=Path, default=DEFAULT_STATE_PATH)
    parser.add_argument("--unique-cfp-url", help="Refuse an ambiguous current CFP reference")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--expected-digests", type=Path, help="JSON source fingerprints for every proposed slug"
    )
    mode.add_argument(
        "--snapshot",
        action="store_true",
        help="Emit source fingerprints from stdin; no state access",
    )
    args = parser.parse_args(argv)

    if args.unique_cfp_url is not None and (args.snapshot or args.expected_digests is None):
        sys.stderr.write("commit-state: unique CFP reference requires guarded commit mode\n")
        return 1

    raw = sys.stdin.read()
    try:
        working_set = json.loads(raw)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"commit-state: stdin is not valid JSON: {exc}\n")
        return 1
    if not isinstance(working_set, dict):
        sys.stderr.write(
            f"commit-state: stdin root is {type(working_set).__name__}, "
            f"expected a JSON object of slug -> record\n"
        )
        return 1
    for slug, record in working_set.items():
        if slug.startswith("_"):
            sys.stderr.write(
                f"commit-state: refusing to write config/heartbeat key {slug!r} — "
                f"`_`-prefixed keys have their own writers (stamp-last-checked.py, "
                f"user-managed config); remove it from the payload and rerun\n"
            )
            return 1

        if not isinstance(record, dict) and not (args.snapshot and record is None):
            sys.stderr.write(
                f"commit-state: record for {slug!r} is {type(record).__name__}, "
                f"expected an object — fix the working set and rerun\n"
            )
            return 1

    if args.unique_cfp_url is not None and (
        not args.unique_cfp_url
        or len(working_set) != 1
        or any(
            not isinstance(record.get("cfp_url"), str)
            or record["cfp_url"].rstrip("/") != args.unique_cfp_url.rstrip("/")
            for record in working_set.values()
        )
    ):
        sys.stderr.write("commit-state: supply one proposed record with the exact CFP reference\n")
        return 1

    if args.snapshot:
        try:
            snapshot = {slug: record_digest(record) for slug, record in working_set.items()}
        except (ValueError, UnicodeEncodeError) as exc:
            sys.stderr.write(
                f"commit-state: cannot fingerprint source snapshot: {exc}; "
                "restore finite UTF-8 records before retrying\n"
            )
            return 1
        print(json.dumps(snapshot))
        return 0

    expected = None
    if args.expected_digests is not None:
        try:
            expected = json.loads(args.expected_digests.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            sys.stderr.write(
                f"commit-state: cannot read source fingerprints: {exc}; "
                "restore the snapshot before retrying\n"
            )
            return 1
        if (
            not isinstance(expected, dict)
            or set(expected) != set(working_set)
            or any(
                not isinstance(value, str) or re.fullmatch(r"[a-f0-9]{64}", value) is None
                for value in expected.values()
            )
        ):
            sys.stderr.write(
                "commit-state: supply one valid source fingerprint per proposed slug "
                "before retrying\n"
            )
            return 1

    try:
        with state_lock.locked(args.state):
            state_absent = False
            try:
                state = json.loads(args.state.read_text(encoding="utf-8"))
            except FileNotFoundError:
                state = {}
                state_absent = True
            except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
                sys.stderr.write(
                    f"commit-state: cannot read {args.state}: "
                    f"{type(exc).__name__}: {exc} — restore or repair the file "
                    f"and rerun\n"
                )
                return 1
            if not isinstance(state, dict):
                sys.stderr.write(
                    f"commit-state: {args.state} root is {type(state).__name__}, "
                    f"expected a JSON object — restore or repair the file and rerun\n"
                )
                return 1

            written = 0
            skipped_user_actioned = 0
            skipped_changed: list[str] = []
            reference_matches = None
            if args.unique_cfp_url is not None:
                reference_matches = {
                    slug
                    for slug, record in state.items()
                    if not slug.startswith("_")
                    and isinstance(record, dict)
                    and isinstance(record.get("cfp_url"), str)
                    and record["cfp_url"].rstrip("/") == args.unique_cfp_url.rstrip("/")
                }
            for slug, record in working_set.items():
                on_disk = state.get(slug)
                # Commit-time re-check against the FRESH read: the agent's
                # copy may predate a user action taken mid-run.
                if isinstance(on_disk, dict) and on_disk.get("user_actioned") is True:
                    skipped_user_actioned += 1
                    continue
                if reference_matches is not None and reference_matches != {slug}:
                    skipped_changed.append(slug)
                    continue
                if expected is not None:
                    try:
                        actual = record_digest(on_disk)
                    except (ValueError, UnicodeEncodeError) as exc:
                        sys.stderr.write(
                            f"commit-state: cannot fingerprint stored source: {exc}; "
                            "repair the state before retrying\n"
                        )
                        return 1
                    if actual != expected[slug]:
                        skipped_changed.append(slug)
                        continue
                state[slug] = record
                written += 1

            # `state_absent` carries the first-run case through an empty
            # working set: the file has to exist for the stampers that
            # follow, so materialize `{}` rather than skipping the write.
            # An existing file with nothing written stays untouched.
            if written or (state_absent and args.unique_cfp_url is None):
                try:
                    _atomic_write_json(args.state, state)
                except OSError as exc:
                    sys.stderr.write(
                        f"commit-state: cannot write {args.state}: {type(exc).__name__}: {exc}\n"
                    )
                    return 1
            total = sum(
                1 for k, v in state.items() if not k.startswith("_") and isinstance(v, dict)
            )
            payload: dict[str, int | list[str]] = {
                "written": written,
                "skipped_user_actioned": skipped_user_actioned,
                "total_records": total,
            }
    except state_lock.LockError as exc:
        sys.stderr.write(f"commit-state: {exc}\n")
        return 1

    if expected is not None:
        payload["skipped_changed"] = skipped_changed

    # Print after releasing the lock — a blocked stdout consumer must not
    # extend the exclusive hold beyond the read-modify-write.
    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
