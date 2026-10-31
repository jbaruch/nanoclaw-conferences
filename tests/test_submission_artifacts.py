"""Private synthetic filesystem/CLI composition; no provider or native actor."""

import hashlib
import importlib
import io
import json
import os
from copy import deepcopy
from pathlib import Path

import pytest

from test_submission_sources import hydrated_mail

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/cfp-submissions/scripts"


@pytest.fixture
def environment(monkeypatch, tmp_path):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    artifacts = importlib.import_module("submission_artifacts")
    owner = importlib.import_module("submissions")
    # Model the Linux fixed /tmp boundary on a private synthetic root. macOS's
    # actual /tmp is a symlink; this does not claim native artifact admission.
    root = tmp_path / "root"
    temporary = root / "tmp"
    temporary.mkdir(parents=True)
    real_open, real_stat = os.open, os.stat

    def open_root(path, flags, mode=0o777, *, dir_fd=None):
        return real_open(root if path == "/" else path, flags, mode, dir_fd=dir_fd)

    def stat_root(path, *, dir_fd=None, follow_symlinks=True):
        return real_stat(
            root if path == "/" else path, dir_fd=dir_fd, follow_symlinks=follow_symlinks
        )

    monkeypatch.setattr(os, "open", open_root)
    monkeypatch.setattr(os, "stat", stat_root)
    monkeypatch.setattr(owner, "LEDGER", tmp_path / "ledger.json")
    return artifacts, owner, temporary


def retained(temporary, index=1, wrapper=None, raw=None):
    capture = deepcopy(hydrated_mail()["collections"][0])
    wrapper = capture["evidence"] if wrapper is None else wrapper
    raw = json.dumps(wrapper, ensure_ascii=False).encode() if raw is None else raw
    artifact_id = f"{index:032x}"
    parent = temporary / ("nanoclaw-mail-evidence-" + artifact_id)
    parent.mkdir(mode=0o700)
    parent.chmod(0o700)
    leaf = parent / "evidence.json"
    fd = os.open(leaf, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, raw)
    finally:
        os.close(fd)
    capture.pop("evidence")
    capture["receipt"] = {
        "schema_version": 1,
        "artifact_id": artifact_id,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return capture, parent, leaf


def request(*captures):
    return {"schema_version": 1, "collections": list(captures)}


def run_cli(owner, packet, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(packet)))
    status = owner.main(["import-mail-artifacts"])
    output = capsys.readouterr()
    return status, output


def test_real_owner_large_body_imports_without_body_stdout_and_removes_known_inputs(
    environment, monkeypatch, capsys
):
    _, owner, temporary = environment
    wrapper = hydrated_mail()["collections"][0]["evidence"]
    private = "PRIVATE-SYNTHETIC-TEXT " * 2000
    wrapper["body_reads"][0]["evidence"]["body"] += " " + private
    capture, parent, _ = retained(temporary, wrapper=wrapper)
    assert capture["receipt"]["bytes"] > 25 * 1024
    status, output = run_cli(owner, request(capture), monkeypatch, capsys)
    assert status == 0 and output.err == "" and len(output.out.encode()) < 1024
    result = json.loads(output.out)
    assert result["recorded"] and result["observations"] == 1
    assert result["artifact_cleanup"] == "removed" and result["artifacts_removed"] == 1
    assert not parent.exists() and private not in output.out
    store = owner.read(owner.LEDGER)
    assert len(store["unresolved"]) == 1
    assert next(iter(store["unresolved"].values()))["status"] == "submitted"
    assert not next(iter(store["coverage"].values()))["complete"]


@pytest.mark.parametrize("damage", ["receipt_hash", "late_wrapper", "duplicate_json"])
def test_whole_batch_holds_before_ledger_commit_and_preserves_all_inputs(
    environment, monkeypatch, capsys, damage
):
    _, owner, temporary = environment
    first, first_parent, _ = retained(temporary)
    bad, parent, leaf = retained(temporary, 2)
    if damage == "receipt_hash":
        bad["receipt"]["sha256"] = "0" * 64
    elif damage == "late_wrapper":
        wrapper = hydrated_mail()["collections"][0]["evidence"]
        wrapper["body_reads"][0]["thread_id"] = "c001"
        raw = json.dumps(wrapper).encode()
        leaf.write_bytes(raw)
        bad["receipt"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    else:
        raw = b'{"schema_version":1,"schema_version":1}'
        leaf.write_bytes(raw)
        bad["receipt"].update(bytes=len(raw), sha256=hashlib.sha256(raw).hexdigest())
    status, output = run_cli(owner, request(first, bad), monkeypatch, capsys)
    assert status == 1 and output.out == "" and not owner.LEDGER.exists()
    assert first_parent.exists() and parent.exists() and leaf.exists()


@pytest.mark.parametrize(
    "damage", ["extra", "path", "upper", "boolean", "zero", "large", "version", "duplicate"]
)
def test_receipt_grammar_never_selects_caller_paths_or_duplicate_inputs(environment, damage):
    artifacts, _, temporary = environment
    capture, parent, _ = retained(temporary)
    proof = capture["receipt"]
    if damage == "extra":
        proof["path"] = "/caller/path"
    elif damage == "path":
        proof["artifact_id"] = "../outside"
    elif damage == "upper":
        proof["artifact_id"] = "A" * 32
    elif damage == "boolean":
        proof["bytes"] = True
    elif damage == "zero":
        proof["bytes"] = 0
    elif damage == "large":
        proof["bytes"] = 524289
    elif damage == "version":
        proof["schema_version"] = 2
    packet = request(capture, deepcopy(capture)) if damage == "duplicate" else request(capture)
    with pytest.raises(artifacts.ArtifactHold):
        artifacts.import_artifacts(packet, lambda _: pytest.fail("unexpected commit"))
    assert parent.exists()


@pytest.mark.parametrize(
    "damage",
    ["parent_mode", "leaf_mode", "hardlink", "leaf_link", "parent_link", "size", "foreign_owner"],
)
def test_private_metadata_and_attachment_holds_before_commit(environment, monkeypatch, damage):
    artifacts, _, temporary = environment
    capture, parent, leaf = retained(temporary)
    if damage == "parent_mode":
        parent.chmod(0o755)
    elif damage == "leaf_mode":
        leaf.chmod(0o644)
    elif damage == "hardlink":
        os.link(leaf, temporary / "extra-link")
    elif damage == "leaf_link":
        original = parent / "original"
        leaf.rename(original)
        leaf.symlink_to(original)
    elif damage == "parent_link":
        original = temporary / "original"
        parent.rename(original)
        parent.symlink_to(original, target_is_directory=True)
    elif damage == "size":
        leaf.write_bytes(leaf.read_bytes() + b" ")
    else:
        monkeypatch.setattr(artifacts.os, "geteuid", lambda: os.getuid() + 1)
    with pytest.raises((artifacts.ArtifactHold, OSError)):
        artifacts.import_artifacts(request(capture), lambda _: pytest.fail("unexpected commit"))
    assert parent.exists()


def test_foreign_replacement_after_known_commit_is_preserved_and_stops_cleanup(environment):
    artifacts, _, temporary = environment
    first, parent, leaf = retained(temporary)
    second, other_parent, other_leaf = retained(temporary, 2)

    def commit(_):
        leaf.rename(parent / "retained-original")
        leaf.write_text("foreign replacement")
        leaf.chmod(0o600)
        return {"recorded": True}

    result = artifacts.import_artifacts(request(first, second), commit)
    assert result["artifact_cleanup"] == "preserved_uncertain"
    assert result["artifacts_removed"] == 0
    assert (
        leaf.read_text() == "foreign replacement" and other_leaf.exists() and other_parent.exists()
    )


def test_unknown_commit_error_and_close_failure_keep_primary_and_close_every_fd(
    environment, monkeypatch
):
    artifacts, _, temporary = environment
    capture, parent, _ = retained(temporary)
    primary, secondary = RuntimeError("synthetic commit fault"), OSError("synthetic close fault")
    original_close = os.close
    closed = []

    def close(fd):
        closed.append(fd)
        original_close(fd)
        if len(closed) == 1:
            raise secondary

    def commit(_):
        raise primary

    monkeypatch.setattr(os, "close", close)
    with pytest.raises(ExceptionGroup) as caught:
        artifacts.import_artifacts(request(capture), commit)
    assert caught.value.exceptions == (primary, secondary) and len(closed) == 4
    assert parent.exists()


def test_unknown_commit_result_preserves_inputs_without_cleanup(environment):
    artifacts, _, temporary = environment
    capture, parent, leaf = retained(temporary)
    with pytest.raises(artifacts.ArtifactHold):
        artifacts.import_artifacts(request(capture), lambda _: {"recorded": False})
    assert parent.exists() and leaf.exists()


def test_two_receipts_validate_before_one_actual_ledger_publication(
    environment, monkeypatch, capsys
):
    _, owner, temporary = environment
    first, first_parent, _ = retained(temporary)
    second, second_parent, _ = retained(temporary, 2)
    publish = owner.publish
    writes = []

    def once(path, store):
        writes.append(path)
        return publish(path, store)

    monkeypatch.setattr(owner, "publish", once)
    status, output = run_cli(owner, request(first, second), monkeypatch, capsys)
    assert status == 0 and writes == [owner.LEDGER]
    assert json.loads(output.out)["artifacts_removed"] == 2
    assert not first_parent.exists() and not second_parent.exists()


def test_file_mutation_during_read_holds_without_commit(environment, monkeypatch):
    artifacts, _, temporary = environment
    capture, parent, leaf = retained(temporary)
    read = os.read
    mutated = False

    def changed(fd, length):
        nonlocal mutated
        chunk = read(fd, length)
        if not mutated:
            mutated = True
            leaf.write_bytes(leaf.read_bytes() + b" ")
        return chunk

    monkeypatch.setattr(os, "read", changed)
    with pytest.raises(artifacts.ArtifactHold):
        artifacts.import_artifacts(request(capture), lambda _: pytest.fail("unexpected commit"))
    assert parent.exists() and leaf.exists()


def test_cleanup_io_failure_reports_known_import_and_stops_deletion(environment, monkeypatch):
    artifacts, _, temporary = environment
    first, _, first_leaf = retained(temporary)
    second, _, second_leaf = retained(temporary, 2)
    calls = []

    def denied(path, *, dir_fd=None):
        calls.append(path)
        raise PermissionError("synthetic cleanup denied")

    monkeypatch.setattr(os, "unlink", denied)
    result = artifacts.import_artifacts(request(first, second), lambda _: {"recorded": True})
    assert result == {
        "recorded": True,
        "artifact_cleanup": "preserved_uncertain",
        "artifacts_removed": 0,
    }
    assert calls == ["evidence.json"] and first_leaf.exists() and second_leaf.exists()
