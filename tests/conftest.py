import importlib.util
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / relpath)
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
