"""Advisory file lock for cfp-state.json read-modify-write sections.

Every cfp-state.json mutator reads the whole file, mutates in memory, and
writes back via temp file + os.replace. The atomic replace prevents
truncation/partial files, but not lost updates: two writers that read the
same old state each write a complete file, and whichever lands second
silently discards the other's changes (jbaruch/nanoclaw-conferences#35).
Main groups run default and maintenance containers concurrently against
the same /workspace/group/ volume, so this is a real interleaving, not a
theoretical one.

This module is the shared write discipline: every read-modify-write of
cfp-state.json runs inside `locked(state_path)`. The lock is an advisory
`fcntl.flock` on a sibling `<name>.lock` file (never on the state file
itself — os.replace swaps the state file's inode, which would drop the
lock mid-write). Readers that only snapshot the file (check-cfps-fetch)
do not need the lock: os.replace guarantees they see a complete old or
complete new file.

The lock file is created on first use and intentionally never unlinked —
removing a lock file while another process holds/awaits its fd reopens
the race the lock exists to close.

Acquisition blocks up to `timeout` seconds (default DEFAULT_TIMEOUT,
overridable per-call or via the CFP_STATE_LOCK_TIMEOUT env var), then
raises LockTimeout. Callers translate LockTimeout into their script's
exit-1 stderr diagnostic contract.

Not a standalone script: importable module only, no entry point. Writer
scripts sys.path-insert their own directory and `import state_lock`.
"""

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path

DEFAULT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.05
LOCK_SUFFIX = ".lock"


class LockTimeout(Exception):
    """Raised when the state lock cannot be acquired within the timeout."""


def lock_path_for(state_path: Path) -> Path:
    return state_path.with_name(state_path.name + LOCK_SUFFIX)


def _effective_timeout(timeout):
    if timeout is not None:
        return float(timeout)
    env = os.environ.get("CFP_STATE_LOCK_TIMEOUT")
    return float(env) if env else DEFAULT_TIMEOUT


@contextmanager
def locked(state_path: Path, timeout: float | None = None):
    """Hold the advisory write lock for `state_path`'s RMW section.

    Blocks up to `timeout` seconds (None → CFP_STATE_LOCK_TIMEOUT env var
    or DEFAULT_TIMEOUT), polling a non-blocking flock. Raises LockTimeout
    on expiry; propagates OSError if the lock file cannot be created."""
    deadline = time.monotonic() + _effective_timeout(timeout)
    fd = os.open(lock_path_for(state_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"could not acquire {lock_path_for(state_path)} within "
                        f"{_effective_timeout(timeout):g}s — another cfp-state writer "
                        f"holds it; retry once it finishes"
                    ) from None
                time.sleep(_POLL_INTERVAL)
        yield
    finally:
        os.close(fd)
