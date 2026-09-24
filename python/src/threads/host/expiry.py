"""The question expiry driver: every open ask_user question past its expires_at (the `questions`
rows, across tenants) has its thread resumed, and the run closes it with "no answer" and goes
on. The row survives restarts, so a question that expired while no host ran is closed on the
first pass after start. The log decides: a question answered meanwhile is left alone."""

import asyncio
from typing import Final

from threads.agents.store import now_ms, open_store
from threads.host.runs import Runner
from threads.host.schedule_pass import isolated
from threads.log import BranchId, ThreadId
from threads.store import LOCAL_TENANT
from threads.store.sql import text_of

EXPIRY_S: Final = 1.0


async def expire(runner: Runner) -> None:
    """One pass: resumes the thread of each question due now."""
    now = now_ms()
    sq = await open_store(runner.store(LOCAL_TENANT))
    rows = await sq.run(
        lambda c: c.execute(
            "SELECT q.tenant_id, b.thread_id, q.branch_id FROM questions q"
            " JOIN branches b ON b.branch_id = q.branch_id"
            " WHERE q.state = 'open' AND q.expires_at <= ?",
            (now,),
        ).fetchall()
    )
    for tenant, thread, branch in rows:
        store = runner.store(text_of(tenant))
        await runner.resume(store, ThreadId(text_of(thread)), BranchId(text_of(branch)))


async def run(runner: Runner) -> None:
    """A pass each second until the host stops; a failed pass is said and the next one runs."""
    while True:
        await isolated("question expiry", lambda: expire(runner))
        await asyncio.sleep(EXPIRY_S)
