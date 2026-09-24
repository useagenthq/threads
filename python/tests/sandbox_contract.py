"""The shared sandbox adapter suite (spec/api.json `Sandbox`, `SandboxSession`): every provider
adapter runs these checks over its mocked backend (sandbox_backend.py).

A provider's test module supplies `Make`: the adapter wired to a FakeBackend through that
provider's mocked transport, under a provider name. Checks assert what reached the backend, so
a fence that runs anywhere but at the transport, or a credential that reaches a sandbox, fails.
"""

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Protocol

from sandbox_backend import FakeBackend
from sandbox_kit import OPEN, KitContext

from threads.adapters.sandboxes.posix import collect
from threads.loop.model import Found, LookupUnknown, NotFound, NotFoundNonfinal
from threads.result import Err, Ok
from threads.sandbox import LooksUpSandbox, LooksUpSnapshot, Sandbox, SandboxSession
from threads.sandbox.manifest import manifest_hash, manifest_of
from threads.sandbox.protocol import ExecOutput, SandboxError
from threads.store.context import CleanupAuthority


class Bundled(Sandbox, LooksUpSandbox, LooksUpSnapshot, Protocol):
    """Every bundled sandbox adapter implements both lookups."""


type Make = Callable[[FakeBackend, str], AbstractAsyncContextManager[Bundled]]
"""The adapter over `backend`, named `name` (its SandboxInfo.provider)."""


@dataclass(frozen=True)
class Harness:
    backend: FakeBackend
    sandbox: Bundled
    secrets: tuple[str, ...]
    """The credentials the adapter holds: none may reach a sandbox."""


type Check = Callable[[Harness], Awaitable[None]]


async def run_check(check: Check, make: Make, secrets: tuple[str, ...]) -> None:
    backend = FakeBackend.scripted()
    async with make(backend, "under_test") as sandbox:
        await check(Harness(backend, sandbox, secrets))


async def session(h: Harness, key: str = "k1") -> SandboxSession:
    made = await h.sandbox.create(key, OPEN)
    assert isinstance(made, Ok), made
    return made.value


async def ran(started: Ok[ExecOutput] | Err[SandboxError]) -> tuple[int, bytes, bytes]:
    assert isinstance(started, Ok), started
    return await collect(started.value)


async def exec_streams_output_and_exit_codes(h: Harness) -> None:
    s = await session(h)
    assert await ran(await s.exec(["echo", "hi"], OPEN, process_key="p1")) == (0, b"hi\n", b"")
    code, _, err = await ran(await s.exec(["nope"], OPEN, process_key="p2"))
    assert (code, b"nope" in err) == (127, True)
    fed = await s.exec(["cat"], OPEN, process_key="p3", stdin=b"from stdin")
    assert await ran(fed) == (0, b"from stdin", b"")


MARKERS = b"A\x01\x01\x01B\x02\x02\x02C\x03\x03\x03" + bytes(range(256)) + b"\x02\x02\x02"
"""Every byte value, and the in-band stream markers some providers demux on."""


async def every_byte_reaches_its_own_stream(h: Harness) -> None:
    s = await session(h)
    out = await s.exec(["cat"], OPEN, process_key="o", stdin=MARKERS)
    assert await ran(out) == (0, MARKERS, b"")
    err = await s.exec(["sh", "-c", "cat >&2"], OPEN, process_key="e", stdin=MARKERS)
    assert await ran(err) == (0, b"", MARKERS)


async def env_is_exact_and_credentials_never_enter(h: Harness) -> None:
    """AGENTS invariant 4: exactly the given env, and the adapter's own
    credentials in no environment and no argv the sandbox ever saw."""
    s = await session(h)
    env = {"FOO": "bar"}
    assert await ran(await s.exec(["printenv"], OPEN, process_key="p", env=env)) == (
        0,
        b"FOO=bar\n",
        b"",
    )
    assert await ran(await s.exec(["printenv"], OPEN, process_key="q")) == (0, b"", b"")
    assert h.backend.envs, "the backend saw no environment at all"
    for secret in h.secrets:
        for seen in h.backend.envs:
            assert all(secret not in k and secret not in v for k, v in seen.items())
        assert all(secret not in arg for argv in h.backend.argvs for arg in argv)


async def files_keep_the_path_contract(h: Harness) -> None:
    s = await session(h)
    assert await s.upload("/workspace/a.txt", b"hi", OPEN) == Ok(None)
    assert await s.download("/workspace/a.txt", OPEN) == Ok(b"hi")
    before = h.backend.requests
    for path, code in [("workspace/a.txt", "invalid_path"), ("/workspace/../etc", "invalid_path")]:
        got = await s.download(path, OPEN)
        assert isinstance(got, Err)
        assert got.error.code == code, path
    assert h.backend.requests == before, "an invalid path reached the provider"
    missing = await s.download("/workspace/b.txt", OPEN)
    assert isinstance(missing, Err)
    assert missing.error.code == "not_found"


async def terminate_never_confirms_what_the_guest_could_forge(h: Harness) -> None:
    """only a provider primitive proves a process group gone. An adapter whose
    provider has none answers unknown (the effect parks) even for a command that detached a
    descendant, and never already_exited from an empty scan."""
    s = await session(h)
    started = await s.exec(["sleep", "100"], OPEN, process_key="long")
    detached = await s.exec(["sh", "-c", "setsid sleep 1000 &"], OPEN, process_key="bg")
    assert isinstance(started, Ok)
    assert isinstance(detached, Ok)
    assert h.sandbox.info.termination == "unconfirmed", "no provider here confirms a group"
    for key in ("long", "bg", "never-ran"):
        assert await s.terminate(key, OPEN) == Ok("unknown")


async def every_operation_is_fenced_at_the_transport(h: Harness) -> None:
    """A stale owner reaches nothing: no request of any operation arrives at the provider."""
    s = await session(h)
    snapshots = bool(h.sandbox.info.capture_classes)
    snap_id = "snap-x"
    if snapshots:
        snap = await s.snapshot("snap-key", OPEN)
        assert isinstance(snap, Ok), snap
        snap_id = snap.value.snapshot_id
    for lost, code in [
        (KitContext(live=False), "stale_epoch"),
        (KitContext(live=False, authority=CleanupAuthority("r", "c")), "cleanup_claim_lost"),
    ]:
        before = h.backend.requests
        refused = [
            await h.sandbox.create("k2", lost),
            await h.sandbox.attach(s.id, lost),
            await s.exec(["echo"], lost, process_key="p"),
            await s.terminate("p", lost),
            await s.upload("/workspace/a", b"", lost),
            await s.download("/workspace/a", lost),
            await s.close(lost),
        ]
        if snapshots:
            refused += [
                await h.sandbox.restore(snap_id, "0" * 64, "k3", lost),
                await h.sandbox.release(snap_id, lost),
                await s.snapshot("k4", lost),
            ]
        codes = [r.error.code if isinstance(r, Err) else "ok" for r in refused]
        assert codes == [code] * len(refused)
        assert isinstance(await h.sandbox.lookup("k1", lost), LookupUnknown)
        assert isinstance(await h.sandbox.lookup_snapshot("snap-key", lost), LookupUnknown)
        assert h.backend.requests == before, "a refused operation reached the provider"
        assert lost.fences >= len(refused)


async def a_snapshot_restores_isolated_and_verified(h: Harness) -> None:
    """F11.1 and the child has the file, its writes don't reach the parent,
    and a wrong manifest hash leaves no child alive."""
    if not h.sandbox.info.capture_classes:
        return
    parent = await session(h)
    assert await parent.upload("/workspace/a.txt", b"v1", OPEN) == Ok(None)
    snap = await parent.snapshot("snap-key", OPEN)
    assert isinstance(snap, Ok), snap
    assert snap.value.provider == h.sandbox.info.provider
    assert snap.value.manifest_hash == manifest_hash(manifest_of({"a.txt": b"v1"}))
    if h.sandbox.info.lookup.snapshot != "none":
        assert await h.sandbox.lookup_snapshot("snap-key", OPEN) == Found(snap.value)
    bad = await h.sandbox.restore(snap.value.snapshot_id, "0" * 64, "bad-key", OPEN)
    assert isinstance(bad, Err)
    assert bad.error.code == "snapshot_manifest_mismatch"
    assert all(b.key != "bad-key" for b in h.backend.boxes.values() if b.alive)
    good = snap.value.manifest_hash
    child = await h.sandbox.restore(snap.value.snapshot_id, good, "restore-key", OPEN)
    assert isinstance(child, Ok), child
    assert await child.value.download("/workspace/a.txt", OPEN) == Ok(b"v1")
    assert await child.value.upload("/workspace/a.txt", b"v2", OPEN) == Ok(None)
    assert await parent.download("/workspace/a.txt", OPEN) == Ok(b"v1")
    missing = await h.sandbox.restore("no-such-snapshot", good, "k-missing", OPEN)
    assert isinstance(missing, Err)
    assert missing.error.code == "snapshot_missing"


async def quiescence_comes_only_from_a_provider_pause_or_stop(h: Harness) -> None:
    """: a capture past a running process is claimed only when the
    provider paused or stopped the whole sandbox for it."""
    if not h.sandbox.info.capture_classes:
        return
    s = await session(h)
    started = await s.exec(["sleep", "100"], OPEN, process_key="bg")
    assert isinstance(started, Ok)
    snap = await s.snapshot("snap-key", OPEN)
    if isinstance(snap, Err):
        assert snap.error.code == "not_quiescent"
        return
    stopped = not any(p.running for p in h.backend.boxes[s.id].processes.values())
    assert h.backend.snaps[snap.value.snapshot_id].frozen or stopped


async def a_lost_create_is_found_by_its_key(h: Harness) -> None:
    h.backend.lose_creates = 1
    lost = await h.sandbox.create("k-lost", OPEN)
    assert isinstance(lost, Err)
    assert lost.error.code in ("unavailable", "timeout")
    found = await h.sandbox.lookup("k-lost", OPEN)
    assert isinstance(found, Found)
    assert found.value.id in h.backend.boxes
    assert h.backend.creates == 1
    nothing = await h.sandbox.lookup("k-never", OPEN)
    final = h.sandbox.info.lookup.create == "final"
    assert nothing == (NotFound() if final else NotFoundNonfinal())


async def attach_then_close_releases(h: Harness) -> None:
    s = await session(h)
    again = await h.sandbox.attach(s.id, OPEN)
    assert isinstance(again, Ok), again
    assert await again.value.close(OPEN) == Ok(None)
    gone = await h.sandbox.attach(s.id, OPEN)
    assert isinstance(gone, Err)
    assert gone.error.code in ("not_found", "resource_unknown")


async def a_snapshot_release_is_idempotent(h: Harness) -> None:
    if not h.sandbox.info.capture_classes:
        return
    s = await session(h)
    snap = await s.snapshot("snap-key", OPEN)
    assert isinstance(snap, Ok), snap
    assert await h.sandbox.release(snap.value.snapshot_id, OPEN) == Ok("released")
    assert await h.sandbox.release(snap.value.snapshot_id, OPEN) == Ok("already_gone")


CHECKS: tuple[Check, ...] = (
    exec_streams_output_and_exit_codes,
    every_byte_reaches_its_own_stream,
    env_is_exact_and_credentials_never_enter,
    files_keep_the_path_contract,
    terminate_never_confirms_what_the_guest_could_forge,
    every_operation_is_fenced_at_the_transport,
    a_snapshot_restores_isolated_and_verified,
    quiescence_comes_only_from_a_provider_pause_or_stop,
    a_lost_create_is_found_by_its_key,
    attach_then_close_releases,
    a_snapshot_release_is_idempotent,
)
