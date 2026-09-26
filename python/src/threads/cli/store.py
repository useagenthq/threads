"""The store commands: timeline, export, import, repair, delete, gc. Each is a thin wrapper over
the typed API or the store (7 and 8)."""

import json
import sys

from threads.adapters.loop_resources import holding
from threads.agents.store import Store, now_ms, open_store, scoped, sqlite
from threads.cli.serve import load
from threads.log import BranchId, ThreadId
from threads.log.jcs import canonicalize
from threads.reduce.handlers import to_json
from threads.result import Err, Ok
from threads.sandbox.ledger import gc as release
from threads.store import deletion, retention
from threads.store.lines import uuid7
from threads.store.sql import text_of
from threads.thread.handle import open_thread
from threads.thread.importing import import_thread


def store_at(path: str) -> Store:
    """The store `--store` names: a directory (SQLite), or a postgres:// URL."""
    if path.startswith(("postgres://", "postgresql://")):
        # Loaded only for a Postgres store: a SQLite user never imports psycopg.
        from threads.postgres import postgres  # noqa: PLC0415

        return postgres(path)
    return sqlite(path)


def _store(path: str, tenant: str) -> Store:
    return scoped(store_at(path), tenant)


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


async def export(path: str, tenant: str, branch: str, bundle: str | None = None) -> int:
    """The branch's JSONL export on stdout, or, with --bundle, a portable bundle directory."""
    store = _store(path, tenant)
    if bundle is not None:
        return await _bundle(store, BranchId(branch), bundle)
    sq = await open_store(store)
    lines = await sq.export(BranchId(branch))
    if isinstance(lines, Err):
        return _fail(f"{lines.error.code}: {lines.error.message}")
    sys.stdout.buffer.write(lines.value)
    return 0


async def _bundle(store: Store, branch: BranchId, into: str) -> int:
    """`--bundle`: Thread.export, the public method, and nothing else."""
    sq = await open_store(store)
    read = await sq.read(branch, now_ms())
    if isinstance(read, Err):
        return _fail(f"{read.error.code}: {read.error.message}")
    thread_id = read.value.fold.thread_id
    if thread_id is None:
        return _fail(f"branch_not_found: no branch {branch}")
    opened = await open_thread(store, thread_id, branch_id=branch)
    if isinstance(opened, Err):
        return _fail(f"{opened.error.code}: {opened.error.message}")
    written = await opened.value.export(into)
    if isinstance(written, Err):
        return _fail(f"{written.error.code}: {written.error.message}")
    print(written.value.path)
    return 0


async def import_(path: str, tenant: str, file: str) -> int:
    """Verifies a bundle or a bare export and stores its exact bytes."""
    stored = await import_thread(_store(path, tenant), file)
    if isinstance(stored, Err):
        seq = "" if stored.error.seq is None else f" at seq {stored.error.seq}"
        return _fail(f"{stored.error.code}{seq}: {stored.error.message}")
    print(stored.value.branch)
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
    root = store_at(path)
    sq = await open_store(root)
    if module is not None:
        served = load(module)
        tenants = await sq.run(
            lambda c: [
                text_of(t)
                for (t,) in c.execute("SELECT DISTINCT tenant_id FROM resources").fetchall()
            ],
            read_only=True,
        )
        # Holds the adapters' connections for the sweep; they are closed when it ends.
        async with holding():
            for sandbox in served.sandboxes():
                for tenant in tenants:
                    await release(await open_store(scoped(root, tenant)), sandbox, now_ms)
    keep = await sq.run(retention.referenced, read_only=True)
    removed = await sq.sweep_artifacts(keep, now_ms() - int(grace_days * 86_400_000))
    print(f"removed {len(removed)} unreferenced artifacts")
    return 0
