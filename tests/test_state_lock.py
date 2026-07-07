"""Unit tests for skills/check-cfps/scripts/state_lock.py.

Locks down the module's documented contract:
  - `locked()` creates the advisory lock file at the `<name>.lock`
    sibling of the state path (and never on the state file itself —
    os.replace swaps the state file's inode).
  - The lock is released on context exit (immediately re-acquirable)
    and the lock file is intentionally left behind (unlinking it would
    reopen the race for a waiter holding the old fd).
  - Contention: a second `locked(..., timeout=0)` while the lock is
    held raises LockTimeout with the actionable "could not acquire"
    diagnostic instead of blocking forever.
  - `timeout=None` defers to the CFP_STATE_LOCK_TIMEOUT env var, so
    callers (the writer scripts pass no timeout) are operator-tunable.

flock conflicts across distinct fds within a single process, so
same-process tests exercise the real contention path without
subprocesses or sleeps.
"""

import time

import pytest


def test_lock_file_created_at_lock_sibling(state_lock, tmp_path):
    """Acquiring the lock creates `<state>.lock` next to the state
    path — never a lock on the state file itself."""
    state_path = tmp_path / "cfp-state.json"
    lock_path = tmp_path / "cfp-state.json.lock"

    assert state_lock.lock_path_for(state_path) == lock_path
    assert not lock_path.exists()
    with state_lock.locked(state_path):
        assert lock_path.exists()
    # The state file itself is never created by locking.
    assert not state_path.exists()


def test_lock_released_and_file_kept_on_context_exit(state_lock, tmp_path):
    """On context exit the flock is released — a timeout=0 re-acquire
    succeeds immediately — while the lock file stays behind (removing
    it would reopen the race for a waiter holding the old fd)."""
    state_path = tmp_path / "cfp-state.json"
    with state_lock.locked(state_path):
        pass
    # Re-acquirable with a zero timeout: the first hold was released.
    with state_lock.locked(state_path, timeout=0):
        pass
    assert state_lock.lock_path_for(state_path).exists()


def test_contention_raises_lock_timeout(state_lock, tmp_path):
    """A second acquirer (separate fd) with timeout=0 gets LockTimeout
    while the first holder is still inside its context."""
    state_path = tmp_path / "cfp-state.json"
    with state_lock.locked(state_path):
        with pytest.raises(state_lock.LockTimeout) as excinfo:
            with state_lock.locked(state_path, timeout=0):
                pass
    assert "could not acquire" in str(excinfo.value)
    assert str(state_lock.lock_path_for(state_path)) in str(excinfo.value)


def test_unwritable_lock_location_raises_lock_error(state_lock, tmp_path):
    """A lock file that cannot be created (here: missing parent
    directory) raises LockError with an actionable message instead of
    letting the raw OSError escape as a traceback. LockTimeout
    subclasses LockError, so callers' single except clause covers both."""
    state_path = tmp_path / "no-such-dir" / "cfp-state.json"

    with pytest.raises(state_lock.LockError) as excinfo:
        with state_lock.locked(state_path):
            pass
    assert "cannot create lock file" in str(excinfo.value)
    assert "state directory" in str(excinfo.value)
    assert issubclass(state_lock.LockTimeout, state_lock.LockError)


def test_invalid_env_timeout_raises_lock_error(state_lock, tmp_path, monkeypatch):
    """A non-numeric CFP_STATE_LOCK_TIMEOUT is an operator typo, not a
    traceback: locked() raises LockError naming the bad value and the
    expected format, before touching the lock file."""
    monkeypatch.setenv("CFP_STATE_LOCK_TIMEOUT", "soon")
    state_path = tmp_path / "cfp-state.json"

    with pytest.raises(state_lock.LockError) as excinfo:
        with state_lock.locked(state_path):
            pass
    assert "invalid CFP_STATE_LOCK_TIMEOUT" in str(excinfo.value)
    assert "'soon'" in str(excinfo.value)
    assert not state_lock.lock_path_for(state_path).exists()


def test_invalid_explicit_timeout_raises_lock_error(state_lock, tmp_path):
    state_path = tmp_path / "cfp-state.json"

    with pytest.raises(state_lock.LockError) as excinfo:
        with state_lock.locked(state_path, timeout="never"):
            pass
    assert "invalid lock timeout" in str(excinfo.value)


def test_flock_unsupported_raises_lock_error(state_lock, tmp_path, monkeypatch):
    """A non-contention OSError from flock (e.g. the filesystem does not
    support it) surfaces as LockError with a repair hint, not a raw
    traceback — only BlockingIOError means 'wait and retry'."""
    state_path = tmp_path / "cfp-state.json"

    def _unsupported(fd, op):
        raise OSError(95, "Operation not supported")

    monkeypatch.setattr(state_lock.fcntl, "flock", _unsupported)
    with pytest.raises(state_lock.LockError) as excinfo:
        with state_lock.locked(state_path):
            pass
    assert "cannot lock" in str(excinfo.value)
    assert "flock" in str(excinfo.value)


def test_env_timeout_override_respected(state_lock, tmp_path, monkeypatch):
    """timeout=None defers to CFP_STATE_LOCK_TIMEOUT. With the env set
    to "0" a contended acquire fails fast instead of blocking for the
    30s DEFAULT_TIMEOUT — the elapsed-time bound proves the env value
    (not the default) drove the deadline."""
    monkeypatch.setenv("CFP_STATE_LOCK_TIMEOUT", "0")
    state_path = tmp_path / "cfp-state.json"
    with state_lock.locked(state_path, timeout=5.0):
        start = time.monotonic()
        with pytest.raises(state_lock.LockTimeout):
            with state_lock.locked(state_path, timeout=None):
                pass
        assert time.monotonic() - start < state_lock.DEFAULT_TIMEOUT / 2
