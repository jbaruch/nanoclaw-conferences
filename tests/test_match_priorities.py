"""Tests for skills/check-cfps/scripts/match-priorities.py.

Path C of jbaruch/nanoclaw-admin#308 — the deterministic prefilter that
PROPOSES priority-interest matches. Locks the two bugs the runtime
heuristic had:

  - keyword match is substring, NOT `\\b` word-boundary (so "JavaCro"
    matches `java`);
  - source match reads the stored `source` field, NOT the URL host (so
    a CFP sourced from javaconferences.org matches even when cfp_url
    points at the conference's own form).

And the contract boundaries: the script proposes on a keyword/source
hit but never applies a `note` exclusion or adds a no-hit match — those
stay LLM judgment in Step 6.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_REL = "skills/check-cfps/scripts/match-priorities.py"

# Fixed priorities config mirroring the documented shape (state-management.md).
PRIORITIES = {
    "priority_interests": [
        {
            "id": "ai",
            "label": "AI",
            "keywords": ["AI", "ML", "LLM", "agent", "GenAI"],
            "sources": [],
        },
        {
            "id": "java",
            "label": "Java",
            "keywords": ["Java", "JVM", "Kotlin", "Spring"],
            "sources": ["javaconferences.org"],
        },
        {
            "id": "agentcon",
            "label": "AgentCon (first-world only)",
            "keywords": ["AgentCon"],
            "sources": [],
            "note": "Priority only when held in a first-world country.",
        },
    ]
}


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / SCRIPT_REL)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {name} from {SCRIPT_REL}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def matcher():
    return _load("match_priorities_under_test")


@pytest.fixture
def priorities_file(tmp_path):
    p = tmp_path / "cfp-priorities.json"
    p.write_text(json.dumps(PRIORITIES))
    return p


def _run(module, monkeypatch, capsys, records, priorities_path):
    monkeypatch.setattr("sys.argv", ["match-priorities.py", "--priorities", str(priorities_path)])
    monkeypatch.setattr("sys.stdin", _StdinJSON(records))
    code = module.main()
    captured = capsys.readouterr()
    return code, captured.out, captured.err


class _StdinJSON:
    """Minimal stdin stub exposing `.read()` returning the JSON text."""

    def __init__(self, obj):
        self._text = json.dumps(obj)

    def read(self, *_a):
        return self._text


# --- the reported misses, now caught deterministically ---------------------


def test_javacro_keyword_substring_not_word_boundary(matcher, priorities_file, monkeypatch, capsys):
    """`\\bjava\\b` failed on "JavaCro"; substring matches it."""
    _, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["java"]


def test_source_field_match_not_url_host(matcher, priorities_file, monkeypatch, capsys):
    """jdd-2026: source=javaconferences.org, cfp_url host jdd.org.pl.
    The match must read `source`, not the URL host."""
    rec = {"name": "JDD 2026", "source": "javaconferences.org", "cfp_url": "https://jdd.org.pl/cfp"}
    _, out, _ = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["java"]


def test_keyword_match_over_bot_notes(matcher, priorities_file, monkeypatch, capsys):
    """All Things Open: name has no keyword, but bot_notes mentions
    'Java/JVM content'."""
    rec = {
        "name": "All Things Open 2026",
        "bot_notes": "Broad dev conf; typically Java/JVM content",
    }
    _, out, _ = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["java"]


def test_multiple_interests_in_config_order(matcher, priorities_file, monkeypatch, capsys):
    rec = {"name": "AI + Java Summit", "source": "javaconferences.org"}
    _, out, _ = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    # ai (keyword) + java (keyword AND source) — config order, deduped.
    assert json.loads(out)[0]["proposed_interests"] == ["ai", "java"]


def test_case_insensitive(matcher, priorities_file, monkeypatch, capsys):
    _, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "deep llm tooling"}], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["ai"]


# --- contract boundaries: script proposes, LLM arbitrates -------------------


def test_agentcon_proposed_despite_note(matcher, priorities_file, monkeypatch, capsys):
    """The script proposes `agentcon` on the keyword hit; honoring the
    `note`'s geo exclusion is the LLM's job in Step 6, not this script's.
    `ai` is also proposed because the `agent` keyword is a substring of
    "AgentCon" — the prefilter proposes liberally and the LLM prunes."""
    rec = {"name": "AgentCon Lagos 2026"}
    _, out, _ = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["ai", "agentcon"]


def test_short_keyword_no_substring_false_positive(matcher, priorities_file, monkeypatch, capsys):
    """Short keywords ("ai", "ml") match on a word boundary, so "Rails"
    must NOT propose `ai` and "HTML5" must NOT propose `ai` via "ml"."""
    records = [{"name": "Rails Conf 2026"}, {"name": "HTML5 Summit"}]
    _, out, _ = _run(matcher, monkeypatch, capsys, records, priorities_file)
    assert [r["proposed_interests"] for r in json.loads(out)] == [[], []]


def test_short_keyword_word_boundary_still_matches(matcher, priorities_file, monkeypatch, capsys):
    """The word-boundary guard still fires when the short keyword stands
    as its own token."""
    records = [{"name": "AI Summit 2026"}, {"name": "Applied ML Workshop"}]
    _, out, _ = _run(matcher, monkeypatch, capsys, records, priorities_file)
    assert [r["proposed_interests"] for r in json.loads(out)] == [["ai"], ["ai"]]


def test_long_keyword_still_substring(matcher, priorities_file, monkeypatch, capsys):
    """Keywords longer than the short threshold keep substring matching
    so compound names like "JavaCro" still hit `java`."""
    _, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == ["java"]


def test_no_keyword_or_source_hit_proposes_empty(matcher, priorities_file, monkeypatch, capsys):
    """Confitura: no keyword, source not in any list. The script proposes
    nothing; the LLM adds `java` by world knowledge in Step 6."""
    rec = {"name": "Confitura 2026", "source": "developers.events"}
    _, out, _ = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    assert json.loads(out)[0]["proposed_interests"] == []


def test_parallel_output_preserves_order(matcher, priorities_file, monkeypatch, capsys):
    records = [{"name": "JavaCro'26"}, {"name": "Nothing Relevant"}, {"name": "LLM Day"}]
    _, out, _ = _run(matcher, monkeypatch, capsys, records, priorities_file)
    result = json.loads(out)
    assert [r["name"] for r in result] == ["JavaCro'26", "Nothing Relevant", "LLM Day"]
    assert [r["proposed_interests"] for r in result] == [["java"], [], ["ai"]]


# --- config edge cases ------------------------------------------------------


def test_missing_config_proposes_empty_for_all(matcher, tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope.json"
    code, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], missing)
    assert code == 0
    assert json.loads(out)[0]["proposed_interests"] == []


def test_empty_file_is_no_policy(matcher, tmp_path, monkeypatch, capsys):
    """A 0-byte config behaves as 'no policy' (`[]`), per the
    'missing or empty ⇒ no policy' contract — not an exit-1 error."""
    empty = tmp_path / "cfp-priorities.json"
    empty.write_text("")
    code, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], empty)
    assert code == 0
    assert json.loads(out)[0]["proposed_interests"] == []


def test_whitespace_only_file_is_no_policy(matcher, tmp_path, monkeypatch, capsys):
    ws = tmp_path / "cfp-priorities.json"
    ws.write_text("   \n\t ")
    code, out, _ = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], ws)
    assert code == 0
    assert json.loads(out)[0]["proposed_interests"] == []


def test_malformed_config_exits_1(matcher, tmp_path, monkeypatch, capsys):
    bad = tmp_path / "cfp-priorities.json"
    bad.write_text("not json {")
    code, _, err = _run(matcher, monkeypatch, capsys, [{"name": "x"}], bad)
    assert code == 1
    assert "malformed" in err
    # Actionable per coding-policy: error-handling — tells the operator what to do.
    assert "remove the file" in err or "Fix the JSON" in err


def test_unreadable_config_exits_1(matcher, tmp_path, monkeypatch, capsys):
    """A non-missing read failure (here: the path is a directory, an
    OSError) exits 1 through the documented diagnostic, not a raw
    traceback."""
    a_dir = tmp_path / "cfp-priorities.json"
    a_dir.mkdir()
    code, _, err = _run(matcher, monkeypatch, capsys, [{"name": "x"}], a_dir)
    assert code == 1
    assert "unreadable" in err


def test_bad_stdin_exits_2(matcher, priorities_file, monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["match-priorities.py", "--priorities", str(priorities_file)])
    monkeypatch.setattr("sys.stdin", _StdinJSON("not-a-list"))
    code = matcher.main()
    err = capsys.readouterr().err
    assert code == 2
    assert "JSON array" in err


def test_non_list_keywords_in_config_exits_1(matcher, tmp_path, monkeypatch, capsys):
    """A bare-string `keywords` would be matched character-by-character —
    reject it at the boundary (exit 1) rather than char-iterate."""
    bad = tmp_path / "cfp-priorities.json"
    bad.write_text(json.dumps({"priority_interests": [{"id": "java", "keywords": "java"}]}))
    code, _, err = _run(matcher, monkeypatch, capsys, [{"name": "JavaCro'26"}], bad)
    assert code == 1
    assert "must be a JSON array" in err
    assert "character-by-character" in err


def test_non_string_record_field_exits_2(matcher, priorities_file, monkeypatch, capsys):
    """A record whose `source` is a list (not a string) is rejected
    (exit 2) rather than crashing on `.lower()`."""
    rec = {"name": "JDD 2026", "source": ["javaconferences.org"]}
    code, _, err = _run(matcher, monkeypatch, capsys, [rec], priorities_file)
    assert code == 2
    assert "must be a string" in err


def test_propose_tolerates_nonstring_fields_as_backstop(matcher):
    """Backstop: even if a non-string field slips past boundary checks,
    `propose()` treats it as absent — never `.lower()`-crashes or
    char-iterates a string keyword set."""
    interests = [
        {"id": "java", "keywords": "java", "sources": "x"},
        {"id": "ai", "keywords": ["AI"]},
    ]
    # name is a list, source is an int, keywords is a bare string — all tolerated.
    result = matcher.propose({"name": ["weird"], "source": 42}, interests)
    assert result == []
