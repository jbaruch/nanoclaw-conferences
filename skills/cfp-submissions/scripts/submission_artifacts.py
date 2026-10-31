"""Finite private Admin receipt reader for the submissions owner; no provider I/O.

import_artifacts(request, commit) accepts request1 with collections containing
family/scope/observed_at/receipt1. It opens only the provider's fixed /tmp
artifact namespace, validates every bound file, and calls commit(source2) once.
The caller owns source2 validation and its one atomic ledger publication.
All descriptors stay held until that operation returns. Only recorded:true is a
known successful import: then remove exact still-bound inputs, never recursively.
On rejection/unknown outcome preserve inputs. Cleanup uncertainty stops further
deletion and returns cleanup metadata; never repeat collection or import.
ArtifactHold is an actionable refusal, OSError is an access failure, and unknown
implementation errors propagate. Source text and private paths never enter output.
The fixed receipt supplies data, not Main/source/owner/approval authentication.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys

MAX_ARTIFACT_BYTES = 512 * 1024
MAX_COLLECTIONS = 20
PREFIX = "nanoclaw-mail-evidence-"
LEAF = "evidence.json"
DIRECTORY = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


class ArtifactHold(ValueError):
    """Preserve invocation evidence and inspect the original result before retry."""


def require(condition):
    if not condition:
        raise ArtifactHold(
            "mail_artifact_binding; preserve evidence and inspect the original import; "
            "do not replay collection or substitute a path"
        )


def shape(value, keys):
    require(isinstance(value, dict) and set(value) == set(keys))


def receipt(value):
    shape(value, {"schema_version", "artifact_id", "bytes", "sha256"})
    require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    require(
        isinstance(value["artifact_id"], str)
        and re.fullmatch(r"[a-f0-9]{32}", value["artifact_id"])
        and type(value["bytes"]) is int
        and 1 <= value["bytes"] <= MAX_ARTIFACT_BYTES
        and isinstance(value["sha256"], str)
        and re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
    )


def identity(info):
    return info.st_dev, info.st_ino


def metadata(info):
    return (
        identity(info),
        info.st_mode,
        info.st_uid,
        info.st_gid,
        info.st_nlink,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def private(info, directory):
    require((info.st_uid, info.st_gid) == (os.geteuid(), os.getegid()))
    require(stat.S_IMODE(info.st_mode) == (0o700 if directory else 0o600))
    require(stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
    if not directory:
        require(info.st_nlink == 1)


def decode(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            require(key not in value)
            value[key] = item
        return value

    try:
        return json.loads(raw, object_pairs_hook=pairs)
    except (ValueError, UnicodeDecodeError, RecursionError) as error:
        raise ArtifactHold(
            "mail_artifact_json; preserve the original artifact and inspect its producer"
        ) from error


def import_artifacts(request, commit):
    shape(request, {"schema_version", "collections"})
    require(type(request["schema_version"]) is int and request["schema_version"] == 1)
    collections = request["collections"]
    require(isinstance(collections, list) and 1 <= len(collections) <= MAX_COLLECTIONS)
    seen = set()
    for collection in collections:
        shape(collection, {"family", "scope", "observed_at", "receipt"})
        receipt(collection["receipt"])
        artifact_id = collection["receipt"]["artifact_id"]
        require(artifact_id not in seen)
        seen.add(artifact_id)

    # Attempt every descriptor close. Preserve a primary operation
    # error together with close failures instead of replacing its identity.
    descriptors = []
    try:

        def opened(path, flags, parent=None):
            fd = os.open(path, flags, dir_fd=parent)
            descriptors.append(fd)
            return fd

        root = opened("/", DIRECTORY)
        temporary = opened("tmp", DIRECTORY, root)
        root_id, tmp_id = identity(os.fstat(root)), identity(os.fstat(temporary))

        def ancestors():
            require(identity(os.stat("/", follow_symlinks=False)) == root_id)
            require(identity(os.fstat(root)) == root_id)
            require(identity(os.stat("tmp", dir_fd=root, follow_symlinks=False)) == tmp_id)
            require(identity(os.fstat(temporary)) == tmp_id)

        held, inputs = [], []
        for collection in collections:
            proof = collection["receipt"]
            name = PREFIX + proof["artifact_id"]
            parent = opened(name, DIRECTORY, temporary)
            parent_info = os.fstat(parent)
            private(parent_info, True)
            require(parent_info.st_dev == tmp_id[0])
            leaf = opened(LEAF, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, parent)
            leaf_info = os.fstat(leaf)
            private(leaf_info, False)
            require(leaf_info.st_dev == parent_info.st_dev)
            require(leaf_info.st_size == proof["bytes"])

            def verify(
                parent=parent, leaf=leaf, name=name, parent_info=parent_info, leaf_info=leaf_info
            ):
                ancestors()
                for info in (
                    os.fstat(parent),
                    os.stat(name, dir_fd=temporary, follow_symlinks=False),
                ):
                    private(info, True)
                    require(metadata(info) == metadata(parent_info))
                for info in (os.fstat(leaf), os.stat(LEAF, dir_fd=parent, follow_symlinks=False)):
                    private(info, False)
                    require(metadata(info) == metadata(leaf_info))

            verify()
            chunks, count = [], 0
            while count <= proof["bytes"]:
                chunk = os.read(leaf, min(65536, proof["bytes"] + 1 - count))
                if not chunk:
                    break
                chunks.append(chunk)
                count += len(chunk)
            raw = b"".join(chunks)
            require(len(raw) == proof["bytes"])
            require(hashlib.sha256(raw).hexdigest() == proof["sha256"])
            verify()
            inputs.append(
                {key: collection[key] for key in ("family", "scope", "observed_at")}
                | {"evidence": decode(raw)}
            )
            held.append((verify, parent, leaf, name, parent_info))

        for verify, *_ in held:
            verify()
        result = commit({"schema_version": 2, "source": "mail", "collections": inputs})
        require(isinstance(result, dict) and result.get("recorded") is True)
        removed = 0
        cleanup = "removed"
        for verify, parent, leaf, name, parent_info in held:
            try:
                verify()
                os.unlink(LEAF, dir_fd=parent)
                require(os.fstat(leaf).st_nlink == 0)
                ancestors()
                current = os.stat(name, dir_fd=temporary, follow_symlinks=False)
                private(current, True)
                require(identity(current) == identity(parent_info))
                require(identity(os.fstat(parent)) == identity(parent_info))
                os.rmdir(name, dir_fd=temporary)
                removed += 1
            except (OSError, ArtifactHold):
                cleanup = "preserved_uncertain"
                break
        return result | {"artifact_cleanup": cleanup, "artifacts_removed": removed}
    finally:
        primary = sys.exception()
        errors = []
        for fd in reversed(descriptors):
            try:
                os.close(fd)
            except OSError as error:
                errors.append(error)
        if errors:
            raise BaseExceptionGroup(
                "mail artifact descriptor cleanup failed; preserve remaining evidence",
                ([primary] if primary is not None else []) + errors,
            )
