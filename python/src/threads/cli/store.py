"""The store commands: timeline, export, import, repair, delete, gc. Each is a thin wrapper over
the typed API or the store (7 and 8)."""

import json
import sys
from pathlib import Path

from threads.adapters.loop_resources import holding
from threads.agents.store import Store, now_ms, open_store, scoped, sqlite
from threads.cli.serve import load
from threads.log import BranchId, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.ledger import gc as release
from threads.store import deletion, retention, verify_export
from threads.store.lines import uuid7
from threads.thread.handle import open_thread


def _store(path: str, tenant: str) -> Store:
    return scoped(sqlite(path), tenant)


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 1


async def timeline(path: str, tenant: str, thread: str, branch: str | None) -> int:
    """One JSON line per step: the event and whether it is a fork point."""
    at = None if branch is None else BranchId(branch)
    opened = await open_thread(_store(path, tenant), ThreadId(thread), branch_id=at)
    if isinstance(opened, Err):
        return _fail(f"{opened.error.code}: {opened.error.message}")
    steps = await opened.value.timeline()
    if isinstance(steps, Err):
        return _fail(f"{steps.error.code}: {steps.error.message}")
    for entry in steps.value.entries:
        line = canonicalize({"event": to_json(entry.event), "fork_point": entry.fork_point})
        print(line.value if isinstance(line, Ok) else json.dumps(line.error))
    return 0


async def export(path: str, tenant: str, branch: str) -> int:
    """The branch's JSONL export, the stored bytes one line each, ending with its head."""
    sq = await open_store(_store(path, tenant))
    lines = await sq.export(BranchId(branch))
    if isinstance(lines, Err):
        return _fail(f"{lines.error.code}: {lines.error.message}")
    sys.stdout.buffer.write(lines.value)
    return 0


async def import_(path: str, tenant: str, file: str) -> int:
    """Verifies an export and stores its exact bytes."""
    verified = verify_export(Path(file).read_bytes(), now_ms())
    if isinstance(verified, Err):
        return _fail(f"{verified.error.code} at seq {verified.error.seq}: {verified.error.message}")
    sq = await open_store(_store(path, tenant))
    stored = await sq.import_log(verified.value)
    if isinstance(stored, Err):
        return _fail(f"{stored.error.code}: {stored.error.message}")
    print(verified.value.segments[-1].header.branch_id)
    return 0


async def repair(path: str, tenant: str, branch: str) -> int:
    """Makes a torn import runnable: log_repaired records where the dropped bytes were."""
    sq = await open_store(_store(path, tenant))
    taken = await sq.repair_torn(BranchId(branch), uuid7(now_ms()), now_ms)
    if isinstance(taken, Err):
        return _fail(f"{taken.error.code}: {taken.error.message}")
    await taken.value.release()
    return 0


async def delete(path: str, tenant: str, thread: str | None) -> int:
    """One thread with everything that can't outlive it, or with no thread every thread of the
    tenant, in one transaction; each leaves a tombstone. A refusal prints `<code>: <message>`."""
    sq = await open_store(_store(path, tenant))
    now = now_ms()
    if thread:
        done = await sq.run(lambda c: deletion.delete_thread(c, tenant, ThreadId(thread), now))
    else:
        done = await sq.run(lambda c: deletion.delete_tenant(c, tenant, now))
    if isinstance(done, Err):
        return _fail(f"{done.error.code}: {done.error.message}")
    print(
        f"deleted {thread} ({done.value} threads)"
        if thread
        else f"deleted {done.value} threads of {tenant}"
    )
    return 0


async def gc(path: str, module: str | None, grace_days: float) -> int:
    """Releases what the ledger says to release through each agent's sandbox adapter (the
    host module's), then sweeps unreferenced artifacts older than the grace period."""
    root = sqlite(path)
    sq = await open_store(root)
    if module is not None:
        served = load(module)
        tenants = await sq.run(
            lambda c: [str(t) for (t,) in c.execute("SELECT DISTINCT tenant_id FROM resources")]
        )
        # Holds the adapters' connections for the sweep; they are closed when it ends.
        async with holding():
            for sandbox in served.sandboxes():
                for tenant in tenants:
                    await release(await open_store(scoped(root, tenant)), sandbox, now_ms)
    keep = await sq.run(retention.referenced)
    removed = retention.sweep(Path(path) / "artifacts", keep, grace_days * 86_400)
    print(f"removed {len(removed)} unreferenced artifacts")
    return 0
