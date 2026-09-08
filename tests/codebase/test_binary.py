"""Verify CBM executable resolution and provenance pinning."""

from __future__ import annotations

import json
from hashlib import sha256

import pytest

from gh_puller.codebase.cbm.binary import CBMBinaryError, resolve_cbm_binary


def _executable(path, body: str = "codebase-memory-mcp test"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' '{body}'\n")
    path.chmod(0o755)
    return path


def _manifest(registry, binary, *, digest: str | None = None):
    actual_digest = sha256(binary.read_bytes()).hexdigest()
    document = {
        "schema_version": 1,
        "binary": {
            "path": str(binary.relative_to(registry)),
            "sha256": digest or actual_digest,
            "bytes": binary.stat().st_size,
        },
        "source": {"commit": "source-commit"},
        "validation": {"commit": "validation-commit"},
        "capabilities": ["granular-delta-controls", "persistent-mcp"],
    }
    path = registry / "accepted.json"
    path.write_text(json.dumps(document))
    return path


def test_explicit_binary_has_highest_precedence(tmp_path):
    explicit = _executable(tmp_path / "explicit")
    configured = _executable(tmp_path / "configured")

    identity = resolve_cbm_binary(
        explicit,
        environ={"GH_PULLER_CODEBASE_CBM_BINARY": str(configured)},
    )

    assert identity.path == explicit.resolve()
    assert identity.source == "explicit"
    assert identity.sha256 == sha256(explicit.read_bytes()).hexdigest()


def test_default_manifest_resolves_content_addressed_binary(tmp_path):
    binary = _executable(tmp_path / "objects" / "digest" / "codebase-memory-mcp")
    manifest = _manifest(tmp_path, binary)

    identity = resolve_cbm_binary(registry=tmp_path, environ={})

    assert identity.path == binary.resolve()
    assert identity.manifest_path == manifest.resolve()
    assert identity.source_commit == "source-commit"
    assert identity.validation_commit == "validation-commit"
    assert identity.capabilities == frozenset({"granular-delta-controls", "persistent-mcp"})


def test_manifest_rejects_digest_mismatch_and_escape(tmp_path):
    binary = _executable(tmp_path / "binary")
    manifest = _manifest(tmp_path, binary, digest="0" * 64)
    with pytest.raises(CBMBinaryError, match="digest mismatch"):
        resolve_cbm_binary(manifest=manifest, environ={})

    document = json.loads(manifest.read_text())
    document["binary"]["path"] = "../binary"
    manifest.write_text(json.dumps(document))
    with pytest.raises(CBMBinaryError, match="escapes"):
        resolve_cbm_binary(manifest=manifest, environ={})


def test_resolved_identity_detects_replacement(tmp_path):
    binary = _executable(tmp_path / "binary")
    identity = resolve_cbm_binary(binary, environ={})
    replacement = _executable(tmp_path / "replacement", "codebase-memory-mcp replacement")
    replacement.replace(binary)

    with pytest.raises(CBMBinaryError, match="changed after resolution"):
        identity.verify_unchanged()
