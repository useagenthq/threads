"""Job `kill-restart-acked-events`: a host process is killed with
SIGKILL at each durable boundary of a channel run, then restarted on the same store (twice, the
provider redelivering its webhook each time). Every acked event is still there, the run goes on
from the log, nothing is sent twice, and a send whose outcome can't be proven parks."""

from pathlib import Path

import pytest
from jobs.drill import acked, expire_leases, finish, kill, log, sends, spawn, wait_at
from jobs.worker import rows

from threads.log import ChannelDeliveryEvent, EffectCommitEvent, UserInputEvent
from threads.reduce import Fold

pytestmark = pytest.mark.jobs

# (stop point, what the fake channel's lookup can prove)
POINTS = [
    ("webhook_ack", "final"),
    ("user_input", "final"),
    ("model_request", "final"),
    ("effect_begin", "final"),
    ("effect_begin", "none"),
    ("sent", "final"),
    ("sent", "none"),
    ("sent", "unknown"),
    ("effect_commit", "final"),
]


@pytest.mark.parametrize(("point", "lookup"), POINTS)
def test_a_killed_host_restarts_from_the_log(tmp_path: Path, point: str, lookup: str) -> None:
    first = spawn("serve", tmp_path, DRILL_WEBHOOK="1", DRILL_STOP_AT=point, DRILL_LOOKUP=lookup)
    wait_at(first, point)
    kill(first)
    expire_leases(tmp_path)
    before = acked(tmp_path)
    sent_before = sends(tmp_path)

    finish(spawn("serve", tmp_path, DRILL_WEBHOOK="1", DRILL_LOOKUP=lookup))
    fold = log(tmp_path)
    assert [e.event_id for e in fold.events][: len(before)] == before
    assert sum(isinstance(e, ChannelDeliveryEvent) for e in fold.events) == 1
    assert sum(isinstance(e, UserInputEvent) for e in fold.events) == 1
    assert {int(str(r["status"])) for r in rows(tmp_path / "acks.jsonl")} == {200}
    keys = sends(tmp_path)
    assert len(keys) == len(set(keys)), "a send was repeated"

    in_doubt = lookup != "final" and point in ("effect_begin", "sent")
    if in_doubt:
        # Unproven: parked, never re-sent, never reported acknowledged.
        assert [p.kind for p in fold.parked] == ["effect"]
        assert keys == sent_before
        assert _reply(fold) not in fold.results
        assert not any(isinstance(e, EffectCommitEvent) for e in fold.events[len(before) :])
    else:
        assert not fold.parked
        assert [k.rsplit(":", 1)[1] for k in keys] == [_reply(fold)]
        assert [str(r["text"]) for r in rows(tmp_path / "sends.jsonl")] == ["done"]

    # A second restart and redelivery finds nothing to do: the same outcome, nothing appended.
    settled = [e.event_id for e in fold.events]
    finish(spawn("serve", tmp_path, DRILL_WEBHOOK="1", DRILL_LOOKUP=lookup))
    assert [e.event_id for e in log(tmp_path).events] == settled
    assert sends(tmp_path) == keys


def _reply(fold: Fold) -> str:
    """The call id of the host's reply to the final response."""
    return next(c for c in fold.calls if c.startswith("send_"))
