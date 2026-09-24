"""A Modal sandbox's file operations, run through its task command router as small shell
commands (the router has no file API): the path is an argument, the data stdin or stdout, and
the exit code names a refusal."""

import uuid
from collections.abc import Mapping
from typing import Final

from threads.adapters.sandboxes.modal.router import Router
from threads.sandbox.protocol import NO_ENV, SandboxError
from threads.sandbox.remote.driver import FileError

UPLOAD: Final = r"""[ -d "$1" ] && exit 21
mkdir -p "$(dirname "$1")" 2>/dev/null
cat > "$1" 2>/dev/null || exit 13
"""
DOWNLOAD: Final = r"""[ -d "$1" ] && exit 21
[ -e "$1" ] || exit 2
[ -r "$1" ] || exit 13
exec cat -- "$1"
"""
_CODES: Mapping[int, SandboxError] = {
    2: SandboxError("not_found", "no such file"),
    13: SandboxError("permission_denied", "permission denied"),
    21: SandboxError("is_directory", "is a directory"),
}


async def upload(router: Router, path: str, data: bytes) -> None:
    exec_id = str(uuid.uuid4())
    argv = ("/bin/sh", "-c", UPLOAD, "threads", path)
    await router.start(exec_id, argv, NO_ENV, "/", stdout=False, stderr=False)
    await router.write(exec_id, data)
    _checked(await router.wait(exec_id))


async def download(router: Router, path: str) -> bytes:
    exec_id = str(uuid.uuid4())
    argv = ("/bin/sh", "-c", DOWNLOAD, "threads", path)
    await router.start(exec_id, argv, NO_ENV, "/", stderr=False)
    data = b"".join([chunk async for chunk in router.read(exec_id)])
    _checked(await router.wait(exec_id))
    return data


def _checked(code: int) -> None:
    if code != 0:
        raise FileError(
            _CODES.get(code, SandboxError("unavailable", f"the file command exited {code}"))
        )
