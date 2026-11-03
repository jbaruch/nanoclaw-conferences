"""Every script command path in this plugin's skills resolves to a co-shipped
script (`rules/nanoclaw-skill-root.md`).

NanoClaw mounts each skill at `/home/node/.claude/skills/tessl__<skill>/`;
a command block naming `.../tessl__<skill>/scripts/<file>` must point at
`skills/<skill>/scripts/<file>` here, for a skill this plugin ships.
"""

from __future__ import annotations

import json
import re
import shlex
from collections.abc import Iterator
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MOUNT = re.compile(r"/home/node/\.claude/skills/([^\s`'\")/]+)(/[^\s`'\")]*)?")
COMMAND = re.compile(
    r"^tessl__(?P<skill>[a-z0-9-]+)/(?P<kind>scripts|references)/(?P<file>[A-Za-z0-9._-]+)$"
)
INTERPRETERS = frozenset({"python", "python3", "bash", "sh", "node"})
SCRIPT_SUFFIXES = (".py", ".sh", ".mjs", ".js")
FENCE = re.compile(r"^\s*```")
INLINE = re.compile(r"`([^`\n]+)`")
DECLARED = re.compile(r"^/home/node/\.claude/skills/tessl__[a-z0-9-]+/scripts/[A-Za-z0-9._-]+$")


def _authored_scope() -> list[Path]:
    files: list[Path] = []
    for skill in ("check-cfps", "nightly-cfp-sync", "conference-followups"):
        files.append(REPO_ROOT / "skills" / skill / "SKILL.md")
        files.extend(sorted((REPO_ROOT / "skills" / skill / "references").glob("*.md")))
    return [f for f in files if f.exists()]


def _mount_paths() -> list[tuple[Path, str]]:
    found = []
    for path in _authored_scope():
        for match in MOUNT.finditer(path.read_text(encoding="utf-8")):
            found.append((path, match.group(1) + (match.group(2) or "")))
    return found


def test_the_scan_finds_the_command_paths():
    # Positive control: the scan is not vacuous.
    assert len(_mount_paths()) >= 10


def test_every_mount_path_names_a_shipped_skill_script_or_explicit_reference():
    manifest = json.loads((REPO_ROOT / ".tessl-plugin" / "plugin.json").read_text(encoding="utf-8"))
    shipped = {Path(s).name for s in manifest["skills"]}
    for source, tail in _mount_paths():
        command = COMMAND.match(tail)
        assert command, (
            f"{source.relative_to(REPO_ROOT)}: {tail} is not a declared native script or reference"
        )
        assert command["skill"] in shipped, (
            f"{source.relative_to(REPO_ROOT)}: {command['skill']} is not shipped"
        )
        target = REPO_ROOT / "skills" / command["skill"] / command["kind"] / command["file"]
        if command["kind"] == "references":
            assert target.suffix == ".md"
            assert target.relative_to(REPO_ROOT).as_posix() in source.read_text(encoding="utf-8")
        assert target.is_file(), f"{source.relative_to(REPO_ROOT)}: {tail} has no co-shipped file"


def test_the_rule_lists_every_shipped_skill():
    manifest = json.loads((REPO_ROOT / ".tessl-plugin" / "plugin.json").read_text(encoding="utf-8"))
    rule = (REPO_ROOT / "rules" / "nanoclaw-skill-root.md").read_text(encoding="utf-8")
    for skill in manifest["skills"]:
        assert f"`{Path(skill).name}`" in rule


def _tokens(line: str) -> list[str]:
    """Shell words of `line`, quotes removed; an unbalanced quote (prose
    or a heredoc body) falls back to whitespace words with quotes stripped."""
    try:
        return shlex.split(line, comments=True)
    except ValueError:
        return [word.strip("'\"") for word in line.split()]


def _invoked(line: str) -> Iterator[str]:
    """Every script `line` runs: the first operand after an interpreter,
    options and all, and any word that is itself a script path."""
    words = _tokens(line)
    for index, word in enumerate(words):
        if Path(word).name in INTERPRETERS:
            operands = [w for w in words[index + 1 :] if not w.startswith("-")]
            if operands:
                yield operands[0]
        elif word.endswith(SCRIPT_SUFFIXES) and "/" in word:
            yield word


def _command_lines(text: str) -> Iterator[str]:
    """Lines inside fenced code blocks, and inline code spans that call an
    interpreter."""
    fenced = False
    for line in text.splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            yield line
            continue
        for span in INLINE.findall(line):
            if any(Path(w).name in INTERPRETERS for w in _tokens(span)):
                yield span


def _invocations() -> list[tuple[Path, str]]:
    found = []
    for path in _authored_scope():
        for line in _command_lines(path.read_text(encoding="utf-8")):
            found.extend((path, script) for script in _invoked(line))
    return found


def test_the_invocation_scan_finds_the_commands():
    # Positive control: the scan is not vacuous.
    assert len(_invocations()) >= 10


def test_every_script_invocation_uses_the_declared_mount_root():
    """The rule allows no other root: a `.tessl/plugins/...`, a repo-relative
    `skills/...`, an abbreviated `.../`, or a `$HOME` path all fail here."""
    for source, script in _invocations():
        assert DECLARED.match(script), (
            f"{source.relative_to(REPO_ROOT)}: `{script}` is not "
            "/home/node/.claude/skills/tessl__<skill>/scripts/<file>"
        )


def test_the_invocation_check_rejects_other_roots():
    """Quoting, options or a computed root never hide a command from the scan."""
    for line in (
        "python3 .tessl/plugins/jbaruch/nanoclaw-conferences/skills/check-cfps/scripts/geo-gate.py",
        "python3 skills/check-cfps/scripts/geo-gate.py --policy p.json",
        "python3 .../run-state.py",
        'python3 "$HOME/.claude/skills/tessl__check-cfps/scripts/run-state.py" sweep',
        "python3 -I 'skills/check-cfps/scripts/geo-gate.py'",
        'bash ".tessl/plugins/jbaruch/nanoclaw-conferences/skills/check-cfps/scripts/x.sh"',
        'python3 "${SKILL_ROOT}/scripts/run-state.py"',
        "python3 $(dirname $0)/run-state.py",
        "/usr/bin/env python3 skills/check-cfps/scripts/geo-gate.py",
        "echo '[]' | skills/check-cfps/scripts/geo-gate.py",
    ):
        scripts = list(_invoked(line))
        assert scripts, line
        assert not all(DECLARED.match(s) for s in scripts), line


def test_the_invocation_check_accepts_the_declared_root_however_quoted():
    for line in (
        "python3 /home/node/.claude/skills/tessl__check-cfps/scripts/run-state.py sweep",
        'python3 -I "/home/node/.claude/skills/tessl__check-cfps/scripts/geo-gate.py"',
        "echo '[]' | python3 '/home/node/.claude/skills/tessl__check-cfps/scripts/geo-gate.py'",
    ):
        scripts = list(_invoked(line))
        assert scripts and all(DECLARED.match(s) for s in scripts), line


def test_the_command_scan_reads_fences_and_interpreter_spans_only():
    text = (
        "See `scripts/run-state.py` for the contract.\n"
        "Run `python3 skills/check-cfps/scripts/run-state.py` by hand.\n"
        "```bash\n"
        'python3 "$HOME/x/scripts/geo-gate.py"\n'
        "```\n"
    )
    assert list(_command_lines(text)) == [
        "python3 skills/check-cfps/scripts/run-state.py",
        'python3 "$HOME/x/scripts/geo-gate.py"',
    ]
