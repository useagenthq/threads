"""Job `two-writers-fenced`: two host processes on one
store go for the same branch. Exactly one holds the lease at a time; a writer whose lease ran out
while it stalled is refused at the transport and appends nothing after the new owner."""

from pathlib import Path

import pytest
from jobs.drill import (
    expire_leases,
    finish,
    kill,
    log,
    one_writer_at_a_time,
    pids,
    release,
    sends,
    spawn,
    wait_at,
)
from jobs.worker import rows

from threads.log import UserInputEvent

pytestmark = pytest.mark.jobs


def seeded(where: Path) -> None:
    """A webhook acked (its item durable in the inbox) by a host that died before consuming it."""
    seed = spawn("serve", where, DRILL_WEBHOOK="1", DRILL_STOP_AT="webhook_ack")
    wait_at(seed, "webhook_ack")
    kill(seed)
    expire_leases(where)


def test_two_hosts_racing_for_one_branch_run_it_once(tmp_path: Path) -> None:
    seeded(tmp_path)
    racers = [spawn("serve", tmp_path, DRILL_GO="1") for _ in range(2)]
    (tmp_path / "go").touch()
    for racer in racers:
        finish(racer)

    fold = log(tmp_path)
    one_writer_at_a_time(fold)
    assert sum(isinstance(e, UserInputEvent) for e in fold.events) == 1
    assert len(sends(tmp_path)) == 1
    # Only the lease holder dispatched: one process made every model call and every send.
    assert len(pids(tmp_path, "model.jsonl") | pids(tmp_path, "sends.jsonl")) == 1


def test_a_stale_writer_is_fenced_at_the_transport(tmp_path: Path) -> None:
    seeded(tmp_path)
    stale = spawn("serve", tmp_path, DRILL_STOP_AT="effect_begin")
    wait_at(stale, "effect_begin")
    # Stalled between effect_begin and its send, long enough for its lease to run out.
    expire_leases(tmp_path)
    owner = spawn("serve", tmp_path)
    finish(owner)
    assert pids(tmp_path, "sends.jsonl") == {owner.pid}

    release(tmp_path)
    finish(stale)
    (refused,) = rows(tmp_path / "refused.jsonl")
    assert refused["pid"] == stale.pid
    (sent,) = rows(tmp_path / "sends.jsonl")
    assert (sent["key"], sent["pid"]) == (refused["key"], owner.pid)

    fold = log(tmp_path)
    one_writer_at_a_time(fold)
    epochs = [e.epoch for e in fold.events]
    # The owner recovered the stale writer's in-doubt send under a newer epoch, and nothing of
    # the stale epoch follows it.
    assert epochs[-1] > epochs[0]
    assert fold.parked == []
