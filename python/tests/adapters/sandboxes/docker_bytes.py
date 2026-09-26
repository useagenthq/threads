"""The bytes the mocked Docker daemon puts on the wire: the multiplexed exec frames and the
tars an archive GET answers with. Kept beside docker_fake.py so that file stays one job —
routing requests onto a FakeBackend."""

import io
import json
import tarfile
from collections.abc import Mapping, Sequence

GENERATION = "boot-0000 4242"
TOOLS = {"sh": True, "env": True, "bash": True, "tar": True, "git": True, "python3": True}


def frame(stream: int, data: bytes) -> bytes:
    return bytes([stream, 0, 0, 0]) + len(data).to_bytes(4, "big") + data


def tools() -> bytes:
    return json.dumps(TOOLS).encode() + b"\n"


def tar_of(files: Mapping[str, bytes], dirs: Sequence[str] = ()) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w") as tar:
        for name in dirs:
            info = tarfile.TarInfo(name)
            info.type, info.mode = tarfile.DIRTYPE, 0o700
            tar.addfile(info)
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), 0o600
            tar.addfile(info, io.BytesIO(data))
    return out.getvalue()
