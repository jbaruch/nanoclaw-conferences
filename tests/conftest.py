import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
    if spec is None or spec.loader is None:
        raise ImportError(
            f"cannot load {name} from {relpath}: confirm {relpath} exists under the "
            f"repo root ({REPO_ROOT}) and update this loader path if the script was "
            "renamed or moved, then rerun pytest"
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def check_cfps_fetch(tmp_path, monkeypatch):
    """Load check-cfps/scripts/check-cfps-fetch.py with the module-level
    STATE_PATH and TRAVEL_PATH redirected at tmp_path. Returned tuple
    is (module, state_path, travel_path) — neither file is created so
    callers can choose between 'present' (write JSON) and 'absent'
    (leave it).

    The script also fetches two upstream feeds via
    `urllib.request.urlopen`; tests patch the global
    `urllib.request.urlopen` (via `monkeypatch.setattr` with a string
    target) to avoid network I/O — the script imports the function
    through `urllib.request`, so global patching reaches it.
    """
    state_path = tmp_path / "cfp-state.json"
    travel_path = tmp_path / "travel-schedule.json"
    module = _load(
        "check_cfps_fetch_under_test",
        "skills/check-cfps/scripts/check-cfps-fetch.py",
    )
    monkeypatch.setattr(module, "STATE_PATH", state_path)
    monkeypatch.setattr(module, "TRAVEL_PATH", travel_path)
    return module, state_path, travel_path


@pytest.fixture
def backfill_source():
    """Load check-cfps/scripts/backfill-source.py.

    The script reads the state file path from `--state-path` (CLI arg) at
    main() time — no module-level paths to monkeypatch. Tests pass the
    path via the argv list to `module.main([...])` and capture stdout/
    stderr via capsys. Returned value is the module itself; callers
    create their own state file under `tmp_path` and feed its path in.
    """
    return _load(
        "backfill_source_under_test",
        "skills/check-cfps/scripts/backfill-source.py",
    )


@pytest.fixture
def stamp_schema_version():
    """Load check-cfps/scripts/stamp-schema-version.py.

    The script reads the state file path from `--state` (CLI arg) at main()
    time — no module-level paths to monkeypatch. Tests pass the path via the
    argv list to `module.main([...])` and capture stdout/stderr via capsys.
    Returned value is the module itself; callers create their own state file
    under `tmp_path` and feed its path in.
    """
    return _load(
        "stamp_schema_version_under_test",
        "skills/check-cfps/scripts/stamp-schema-version.py",
    )


@pytest.fixture
def audit_sessionize_key_drift():
    """Load check-cfps/scripts/audit-sessionize-key-drift.py.

    Like `backfill_source`, the script takes `--state-path` as a CLI
    arg and writes JSON to stdout — no module-level paths to
    monkeypatch. Tests call `module.main([...])` with a tmp_path-based
    state file and capture stdout/stderr via capsys.
    """
    return _load(
        "audit_sessionize_key_drift_under_test",
        "skills/check-cfps/scripts/audit-sessionize-key-drift.py",
    )


@pytest.fixture
def dedup_by_url():
    """Load check-cfps/scripts/dedup-by-url.py.

    Like `backfill_source`, the script takes `--state-path` as a CLI
    arg and writes JSON to stdout — no module-level paths to
    monkeypatch. Tests call `module.main([...])` with a tmp_path-based
    state file and capture stdout/stderr via capsys.
    """
    return _load(
        "dedup_by_url_under_test",
        "skills/check-cfps/scripts/dedup-by-url.py",
    )


@pytest.fixture
def prepare_sessionize_batch():
    """Load check-cfps/scripts/prepare-sessionize-batch.py.

    Pure stdin->stdout JSON script; no module-level paths to redirect.
    Tests call `module.prepare([...])` directly for the transform, or
    drive `module.main([])` with an `io.StringIO` stdin + capsys for the
    I/O contract. The module reuses `infer_source` from the sibling
    backfill-source.py at import time, so loading it exercises that
    import path too."""
    return _load(
        "prepare_sessionize_batch_under_test",
        "skills/check-cfps/scripts/prepare-sessionize-batch.py",
    )


@pytest.fixture
def stamp_last_checked():
    """Load check-cfps/scripts/stamp-last-checked.py.

    The script reads the state file path from `--state` (CLI arg) at main()
    time — no module-level paths to monkeypatch. Tests pass the path via the
    argv list to `module.main([...])` and capture stdout/stderr via capsys.
    The run timestamp comes from `module.datetime.now(timezone.utc)`; tests
    that assert the exact stamp monkeypatch `module.datetime` with a frozen
    subclass (same idiom as test_check_cfps_fetch's `checked_at`)."""
    return _load(
        "stamp_last_checked_under_test",
        "skills/check-cfps/scripts/stamp-last-checked.py",
    )


@pytest.fixture
def run_state(tmp_path, monkeypatch):
    """Load check-cfps/scripts/run-state.py with the run directory pinned
    under tmp_path via the `CFP_RUN_STATE_DIR` env override the script reads.

    Returns (module, run_dir). The dir is NOT created up front so callers
    can exercise the absent-store path. `run_date` comes from
    `module.datetime.now(timezone.utc)`; tests that cross a day boundary
    monkeypatch `module.datetime` with a frozen subclass."""
    run_dir = tmp_path / "cfp-run"
    monkeypatch.setenv("CFP_RUN_STATE_DIR", str(run_dir))
    module = _load(
        "run_state_under_test",
        "skills/check-cfps/scripts/run-state.py",
    )
    return module, run_dir


@pytest.fixture
def apply_sessionize_results():
    """Load check-cfps/scripts/apply-sessionize-results.py.

    Pure stdin->stdout JSON script; tests call `module.apply_results(
    prep, results)` / `module.decide(entry, result)` directly, or drive
    `module.main([])` with an `io.StringIO` stdin + capsys for the I/O
    contract."""
    return _load(
        "apply_sessionize_results_under_test",
        "skills/check-cfps/scripts/apply-sessionize-results.py",
    )


@pytest.fixture
def verify_sessionize(tmp_path, monkeypatch):
    """Load check-cfps/scripts/verify-sessionize.py with the run-state dir
    pinned under tmp_path via `CFP_RUN_STATE_DIR` (where the evidence marker
    lands). Returns (module, run_dir); the dir is NOT created up front.

    The driver reuses prepare/apply via sibling importlib load, makes the
    per-slug events call through the module-global `urllib.request.urlopen`
    (tests monkeypatch it, the check-cfps-fetch idiom), reads
    `SESSIONIZE_EVENT_API_KEY`/`SESSIONIZE_API_BASE` at main() time, and stamps
    `run_date` from `module.datetime.now(timezone.utc)` (frozen-subclass
    monkeypatch to cross a day boundary). Tests drive `module.drive(...)`
    directly or `module.main([])` with an `io.StringIO` stdin + capsys."""
    run_dir = tmp_path / "cfp-run"
    monkeypatch.setenv("CFP_RUN_STATE_DIR", str(run_dir))
    monkeypatch.delenv("SESSIONIZE_EVENT_API_KEY", raising=False)
    monkeypatch.delenv("SESSIONIZE_API_BASE", raising=False)
    module = _load(
        "verify_sessionize_under_test",
        "skills/check-cfps/scripts/verify-sessionize.py",
    )
    return module, run_dir


@pytest.fixture
def discover_open_cfps(tmp_path, monkeypatch):
    """Load check-cfps/scripts/discover-open-cfps.py with `CFP_RUN_STATE_DIR`
    and the Sessionize env cleared. Returns (module, state_path); the state
    file is NOT created up front so callers choose present/absent.

    Like verify-sessionize, it fetches via the module-global
    `urllib.request.urlopen` (monkeypatched in tests) and reads
    `SESSIONIZE_SPEAKER_KEY`/`SESSIONIZE_API_BASE` at main() time. Tests call
    `module.build_candidates(events, existing_slugs)` directly for the parse,
    or drive `module.main([...])` with `--state` + capsys for the I/O
    contract."""
    state_path = tmp_path / "cfp-state.json"
    monkeypatch.delenv("SESSIONIZE_SPEAKER_KEY", raising=False)
    monkeypatch.delenv("SESSIONIZE_API_BASE", raising=False)
    module = _load(
        "discover_open_cfps_under_test",
        "skills/check-cfps/scripts/discover-open-cfps.py",
    )
    return module, state_path
