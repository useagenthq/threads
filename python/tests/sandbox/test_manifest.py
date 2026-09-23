"""Snapshot manifests order paths by UTF-16 code units (spec/conformance/vectors)."""

import json
from pathlib import Path

from threads.sandbox.fake_session import manifest_hash, manifest_of

VECTOR = (
    Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors" / "manifest-order.json"
)


def test_manifest_matches_the_shared_vector() -> None:
    vector = json.loads(VECTOR.read_text(encoding="utf-8"))
    files = {f["path"]: f["data"].encode() for f in vector["files"]}
    manifest = manifest_of(files)
    assert [e["path"] for e in manifest] == vector["manifest_paths"]
    assert manifest_hash(manifest) == vector["manifest_hash"]
