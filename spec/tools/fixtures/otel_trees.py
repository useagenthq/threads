# pyright: strict
"""OpenTelemetry goldens across branches and threads: a fork exports only its own segment, and
a subagent's turn joins its parent's trace (or keeps its own root when the parent is gone)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, ALICE, BRANCH, CHILD, DAY, MODEL, NOW, PARAMS, THREAD, obj, sha, text
from .jcs import Obj, canonical
from .log import Log
from .otel_case import TENANT, Sync, write
from .otel_expect import Ctx, Span
from .otel_shapes import chat_of, tool_of, turn_of
from .otel_turns import read_spans
from .pieces import READ_FILE, answer, call, read_turn, result, snapshot, started, user
from .teams import catalog_specs

if TYPE_CHECKING:
    import pathlib

FORK = "0192b000-0000-7000-8000-000000000003"
CHILD_THREAD = "0192a000-0000-7000-8000-0000000000c1"
SPAWN = catalog_specs(("spawn_agent",))


def build(root: pathlib.Path) -> None:
    _fork(root)
    _subagent(root)


def _answer_spans(ctx: Ctx, log: Log) -> list[Span]:
    """The turn and chat spans of the last `user` + `answer`."""
    e = log.events
    t = turn_of(ctx, e[-4], e[-1])
    return [t, chat_of(ctx, t, e[-3], e[-2])]


def _fork(root: pathlib.Path) -> None:
    parent = Log()
    started(parent, [READ_FILE])
    read_turn(parent)
    user(parent, "Thanks.")
    answer(parent, "You're welcome.")
    main = Ctx(TENANT, BRANCH)
    parent_spans = [*read_spans(main, parent, 2), *_answer_spans(main, parent)]
    snapshot(parent, NOW + DAY)
    at = parent.seq
    fork = parent.fork(at, FORK, "sbx_child_01", epoch=2)
    user(fork, "Try another answer.")
    answer(fork, "Here is another.")
    write(
        root,
        "otel-fork-no-reexport",
        "A parent with two finished turns is exported, then forked at its snapshot, and the "
        "fork runs one turn. The fork's first sync starts at its fork point and sends only its "
        "own turn, whose span ids hash the fork's branch id, in its own trace.",
        [parent, fork],
        [
            Sync(BRANCH, at, 0, parent_spans),
            Sync(FORK, fork.seq, at, _answer_spans(Ctx(TENANT, FORK), fork)),
        ],
    )


def _child(spawned: Obj) -> Log:
    log = Log(CHILD, thread=CHILD_THREAD)
    cfg: Obj = {
        "agent_name": "reviewer",
        "instructions": "You review diffs.",
        "model": MODEL,
        "model_params": PARAMS,
        "adapter": ADAPTER,
        "tools": [],
    }
    parent: Obj = {
        "thread_id": THREAD,
        "branch_id": BRANCH,
        "event_id": spawned["event_id"],
        "relation": "subagent",
    }
    log.add("thread_started", {**cfg, "config_hash": sha(canonical(cfg)), "parent": parent})
    prompt: Obj = {"source": "parent_agent", "text": "Review the diff."}
    log.add("user_input", prompt, actor="host", principal=ALICE)
    answer(log, "LGTM.")
    return log


def _subagent(root: pathlib.Path) -> None:
    parent = Log()
    started(parent, SPAWN)
    user(parent, "Review the diff.")
    call(parent, "spawn_agent", {"agent": "reviewer", "prompt": "Review the diff."})
    spawned = parent.add(
        "agent_spawned",
        {
            "call_id": "call_1",
            "child_thread_id": CHILD_THREAD,
            "agent_name": "reviewer",
            "mode": "foreground",
            "isolation": "none",
        },
    )
    child = _child(spawned)
    finished: Obj = {
        "child_thread_id": CHILD_THREAD,
        "status": "completed",
        "output_ref": parent.art(b"LGTM.", "text/plain"),
        "usage": {"input_tokens": 90, "output_tokens": 10},
    }
    parent.add("agent_finished", finished)
    result(parent, "call_1", "LGTM.")
    answer(parent, "The reviewer says LGTM.")
    main = Ctx(TENANT, BRANCH)
    e = parent.events
    t = turn_of(main, e[1], e[-1])
    eclass = text(obj(SPAWN[0])["effect_class"])
    spawn = tool_of(main, t, parent, 5, 9, eclass)
    parent_spans = [t, chat_of(main, t, e[2], e[3]), spawn, chat_of(main, t, e[9], e[10])]
    kid = Ctx(TENANT, CHILD, agent="reviewer")
    c = child.events
    joined = turn_of(kid, c[1], c[-1], trace=spawn.trace, parent=spawn.id)
    write(
        root,
        "otel-subagent-same-trace",
        "A subagent's first turn is parented to the spawn call's tool span, found through "
        "thread_started.parent and the agent_spawned's call_id, in the parent's trace.",
        [parent, child],
        [
            Sync(CHILD, child.seq, 0, [joined, chat_of(kid, joined, c[2], c[3])]),
            Sync(BRANCH, parent.seq, 0, parent_spans),
        ],
    )
    alone = turn_of(kid, c[1], c[-1], parent_missing=True)
    write(
        root,
        "otel-subagent-parent-missing",
        "The same child with its parent thread gone: its turn keeps its own root and is "
        "marked threads.parent_missing.",
        [child],
        [Sync(CHILD, child.seq, 0, [alone, chat_of(kid, alone, c[2], c[3])])],
    )
