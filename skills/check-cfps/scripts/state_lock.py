"""Advisory file lock for cfp-state.json read-modify-write sections.

Every cfp-state.json mutator reads the whole file, mutates in memory, and
writes back via temp file + os.replace. The atomic replace prevents
truncation/partial files, but not lost updates: two writers that read the
same old state each write a complete file, and whichever lands second
silently discards the other's changes (jbaruch/nanoclaw-conferences#35).
Main groups run default and maintenance containers concurrently against
the same /workspace/group/ volume, so this is a real interleaving, not a
theoretical one.

This module is the shared write discipline: every cfp-state.json writer
in this tile runs its read-modify-write inside `locked(state_path)` —
the maintenance scripts (backfill-source, backfill-name, dedup-by-url,
expire-cfps, stamp-schema-version, stamp-last-checked) and the Step 8
committer (commit-state.py), which replaces direct agent-side writes.
The lock is an advisory `fcntl.flock` on a sibling `<name>.lock` file
(never on the state file itself — os.replace swaps the state file's
inode, which would drop the lock mid-write). Readers that only snapshot
the file (check-cfps-fetch) do not need the lock: os.replace guarantees
they see a complete old or complete new file. The one writer outside
this tile, morning-brief's `--mark-shown` (nanoclaw-admin), must adopt
the same lock file to be fully covered — tracked as a follow-up issue
in this repo.

The lock file is created on first use and intentionally never unlinked —
removing a lock file while another process holds/awaits its fd reopens
the race the lock exists to close.

Acquisition blocks up to `timeout` seconds (default DEFAULT_TIMEOUT,
overridable per-call or via the CFP_STATE_LOCK_TIMEOUT env var), then
raises LockTimeout. A lock file that cannot be created (missing state
directory, non-writable filesystem) raises LockError with an actionable
message. LockTimeout subclasses LockError, so callers catch LockError
once and translate it into their script's exit-1 stderr diagnostic
contract — no lock failure ever escapes as a traceback.

Not a standalone script: importable module only, no entry point. Writer
scripts load it from the sibling file via importlib
(`spec_from_file_location`), the same pattern backfill-name.py uses for
its dedup-by-url helpers.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.05
LOCK_SUFFIX = ".lock"


class LockError(Exception):
    """Raised when the state lock cannot be obtained — creation failure
    or timeout. Catch this to honor the exit-1 diagnostic contract."""


class LockTimeout(LockError):
    """Raised when the state lock cannot be acquired within the timeout."""


def lock_path_for(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + LOCK_SUFFIX)


def _effective_timeout(timeout):
    if timeout is not None:
        try:
            value = float(timeout)
        except (TypeError, ValueError) as exc:
            raise LockError(
                f"invalid lock timeout {timeout!r}: expected non-negative "
                f"numeric seconds — fix the caller's timeout argument"
            ) from exc
        if value < 0:
            raise LockError(
                f"invalid lock timeout {timeout!r}: expected non-negative "
                f"numeric seconds — fix the caller's timeout argument"
            )
        return value
    env = os.environ.get("CFP_STATE_LOCK_TIMEOUT")
    if not env:
        return DEFAULT_TIMEOUT
    try:
        value = float(env)
    except ValueError as exc:
        raise LockError(
            f"invalid CFP_STATE_LOCK_TIMEOUT value {env!r}: expected "
            f"non-negative numeric seconds (e.g. 30) — fix or unset the "
            f"environment variable"
        ) from exc
    if value < 0:
        raise LockError(
            f"invalid CFP_STATE_LOCK_TIMEOUT value {env!r}: expected "
            f"non-negative numeric seconds (e.g. 30) — fix or unset the "
            f"environment variable"
        )
    return value


@contextmanager
def locked(state_path: Path, timeout: float | None = None):
    """Hold the advisory write lock for `state_path`'s RMW section.

    Blocks up to `timeout` seconds (None → CFP_STATE_LOCK_TIMEOUT env var
    or DEFAULT_TIMEOUT), polling a non-blocking flock. Raises LockTimeout
    on expiry and LockError when the lock file cannot be created, so
    callers handle every lock failure through one except clause."""
    effective = _effective_timeout(timeout)
    deadline = time.monotonic() + effective
    try:
        fd = os.open(lock_path_for(state_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        raise LockError(
            f"cannot create lock file {lock_path_for(state_path)}: "
            f"{type(exc).__name__}: {exc} — check that the state directory "
            f"exists and is writable, then rerun"
        ) from exc
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire {lock_path_for(state_path)} within "
                        f"{effective:g}s — another cfp-state writer "
                        f"holds it; retry once it finishes"
                    ) from None
                time.sleep(_POLL_INTERVAL)
            except OSError as exc:
                # e.g. flock unsupported on the underlying filesystem —
                # contention is retried above, everything else is terminal.
                raise LockError(
                    f"cannot lock {lock_path_for(state_path)}: "
                    f"{type(exc).__name__}: {exc} — the filesystem may not "
                    f"support flock; move the state file to one that does"
                ) from exc
        yield
    finally:
        os.close(fd)
