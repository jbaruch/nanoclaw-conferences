"""Repository automation stays outside the published plugin."""

import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_renovate_config_is_excluded_from_plugin():
    # Evaluate .tesslignore's gitignore patterns, rather than checking literal text.
    result = subprocess.run(
        [
            "git",
            "-c",
            f"core.excludesFile={REPO_ROOT / '.tesslignore'}",
            "check-ignore",
            "--no-index",
            "renovate.json",
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or ".tesslignore does not exclude renovate.json"
