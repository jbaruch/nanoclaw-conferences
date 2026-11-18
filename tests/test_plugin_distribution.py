"""Repository automation and plugin distribution contracts."""

import json
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_renovate_updates_do_not_require_dashboard_approval():
    config = json.loads((REPO_ROOT / "renovate.json").read_text())
    assert not config.get("dependencyDashboardApproval", False)
    assert all(
        not rule.get("dependencyDashboardApproval", False) for rule in config["packageRules"]
    )
    assert config["prConcurrentLimit"] == 5


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
