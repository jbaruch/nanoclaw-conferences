"""Repository automation and plugin distribution contracts."""

import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_publish_workflow_pin_has_renovate_tracking_ref():
    workflow = (REPO_ROOT / ".github/workflows/publish.yml").read_text()
    # Renovate disables bare SHA references without a branch or tag comment.
    assert re.search(
        r"uses: jbaruch/coding-policy/\.github/workflows/publish-plugin\.yml"
        r"@[0-9a-f]{40}[ \t]+#[ \t]+\S+",
        workflow,
    ), "The publish workflow SHA needs a branch/tag comment for Renovate renewal"


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
