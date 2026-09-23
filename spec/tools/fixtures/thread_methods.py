# pyright: strict
"""Thread methods: replay of a recorded thread, a requested compaction (Thread.compact) and
output styles (Thread.setOutputStyle). Semantic rules 29 and 30."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, NOW, eid, num, sha, tokens
from .jcs import canonical
from .log import Log, reduce
from .pieces import (
    FINAL,
    READ_FILE,
    TAIL,
    answer,
    case,
    read_turn,
    reject,
    render_case,
    started,
    user,
    write_case,
)
from .policies import CONTEXT, PRIMARY_SETTINGS, SMALL_SETTINGS, policy

if TYPE_CHECKING:
    import pathlib

    from .jcs import JsonValue, Obj

FAM = "context_compaction"
SUMMARY = "The user asked what README.md holds (one heading, demo)."
CONCISE = "Answer in at most three sentences."
STYLES: Obj = {"concise": CONCISE}
KEEP = "Keep the open invoice numbers."


def build(root: pathlib.Path) -> None:
    _replay(root)
    _requested(root)
    _old_guide(root)
    _request_rejections(root)
    _answer_rejections(root)
    _breaker_open(root)
    _recorded_response(root)


def base(**sections: JsonValue) -> Log:
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT, **sections))
    read_turn(log)
    return log


def _request(log: Log, instructions: str | None = None) -> Obj:
    data: Obj = {} if instructions is None else {"instructions": instructions}
    return log.add("compaction_requested", data, actor="user", principal=ALICE)


def style(log: Log, name: str, body: str, actor: str = "user") -> Obj:
    data: Obj = {
        "source": "output_style",
        "trust": "trusted_instruction",
        "origin": {"id": name},
        "text": body,
    }
    if actor == "user":
        return log.add("injected", data, actor=actor, principal=ALICE)
    return log.add("injected", data, actor=actor)


def _first_input(log: Log) -> Obj:
    return next(e for e in log.events if e["type"] == "user_input")


def summarize(log: Log, request: Obj | None = None) -> Obj:
    """The side request (naming `request` when given) and its recorded summary."""
    side = log.model_request(compaction=True, cause=request)
    log.model_response(side, [{"type": "text", "text": SUMMARY}], "end_turn", tokens(900, 40))
    return side


def compacted(
    log: Log, bounds: tuple[Obj, Obj], side: Obj, request: Obj | None = None, trigger: str = ""
) -> Obj:
    """compacted over `bounds`, answering `request` when given."""
    first, last = bounds
    data: Obj = {
        "from_seq": first["seq"],
        "to_seq": last["seq"],
        "from_event_id": first["event_id"],
        "to_event_id": last["event_id"],
        "summary_ref": log.art(SUMMARY.encode(), "text/plain"),
        "summary_request_event_id": side["event_id"],
        "trigger": trigger or ("threshold" if request is None else "manual"),
    }
    if request is not None:
        data["cause_event_id"] = request["event_id"]
    return log.add("compacted", data)


def _answered(log: Log, request: Obj, trigger: str = "") -> Obj:
    """The requested compaction over [first input, request - 1]."""
    side = summarize(log, request)
    before = log.events[num(request["seq"]) - 2]
    return compacted(log, (_first_input(log), before), side, request, trigger)


def _replay(root: pathlib.Path) -> None:
    log = base(output_styles=STYLES)
    first, last = log.events[1], log.events[-1]
    log.add(
        "settings_changed",
        {"reason": "user", "settings": SMALL_SETTINGS},
        actor="user",
        principal=ALICE,
    )
    tools: list[JsonValue] = [READ_FILE]
    log.add("tools_changed", {"tools": tools, "tools_hash": sha(canonical(tools))})
    style(log, "concise", CONCISE)
    user(log, "Continue.")
    compacted(log, (first, last), summarize(log))
    answer(log, "Still one heading.")
    log.add(
        "settings_changed",
        {"reason": "user", "settings": PRIMARY_SETTINGS},
        actor="user",
        principal=ALICE,
    )
    user(log, "Thanks.")
    render_case(
        root,
        (
            "replay-render-equals-recorded",
            FAM,
            "Thread.replay: every recorded request of a thread with two settings epochs, a "
            "tools_changed, a compaction and a trusted injection re-renders to its request_ref "
            "bytes and declares its own epoch's line 0 (render step 3), with no model call.",
        ),
        log,
    )


def _requested(root: pathlib.Path) -> None:
    log = base()
    request = _request(log, KEEP)
    user(log, "What changed since?")
    _answered(log, request)
    render_case(
        root,
        (
            "compaction-manual-requested",
            FAM,
            "Thread.compact records compaction_requested with instructions while idle. The next "
            "run's side request names it: its history ends at the request, so the new input is "
            "not summarized, and its instruction line carries the instructions. compacted"
            "{trigger: manual} covers the first input to the event before the request; the next "
            "turn request renders the summary, then the new input.",
        ),
        log,
    )


def _old_guide(root: pathlib.Path) -> None:
    log = base()
    user(log, "Next?")
    log.add(
        "hook_decision",
        {
            "extension": "notes",
            "hook": "before_compact",
            "decision": "guide",
            "reason": "Keep the old notes.",
        },
    )
    side = log.model_request(compaction=True)
    failed: Obj = {"provider_outcome": "failed", "reason": "server_error"}
    log.add("model_attempt_abandoned", {"request_event_id": side["event_id"], **failed})
    data: Obj = {"stage": "summary", "reason": "model_error", "request_event_id": side["event_id"]}
    log.add("compaction_failed", data)
    answer(log, "Nothing new.")
    request = _request(log)
    user(log, "Go on.")
    _answered(log, request)
    render_case(
        root,
        (
            "compaction-manual-old-guide-excluded",
            FAM,
            "A before_compact guide recorded for an earlier, failed compaction is not carried "
            "into a later requested compaction: its side request's instruction line takes only "
            "guides appended after the request, so here it is the fixed text alone.",
        ),
        log,
    )


def _rule(root: pathlib.Path, name: str, desc: str, log: Log) -> None:
    reject(root, (name, "log", desc + " Semantic rule 30: invalid_transition."), log)


def _request_rejections(root: pathlib.Path) -> None:
    log = base()
    _request(log)
    _request(log)
    _rule(root, "compaction-request-twice-rejected", "A second unanswered request.", log)
    log = base()
    user(log, "Still going.")
    _request(log)
    _rule(root, "compaction-request-in-open-turn-rejected", "A request inside an open turn.", log)
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    _request(log)
    _rule(root, "compaction-request-no-input-rejected", "A request with nothing to compact.", log)


def _answer_rejections(root: pathlib.Path) -> None:
    log = base()
    _request(log)
    user(log, "Go on.")
    log.add("compaction_failed", {"stage": "summary", "reason": "still_over_threshold"})
    _rule(
        root,
        "compaction-threshold-answers-request-rejected",
        "A compaction outcome that doesn't name the unanswered request.",
        log,
    )
    log = base()
    user(log, "Go on.")
    orphan: Obj = {"stage": "summary", "reason": "model_error", "cause_event_id": eid(2)}
    log.add("compaction_failed", orphan)
    _rule(root, "compaction-cause-orphan-rejected", "A cause that names no open request.", log)
    log = base()
    user(log, "Anything else?")
    answer(log, "No.")
    request = _request(log)
    user(log, "Go on.")
    side = summarize(log, request)
    second = next(e for e in log.events[3:] if e["type"] == "user_input")
    before = log.events[num(request["seq"]) - 2]
    compacted(log, (second, before), side, request)
    _rule(
        root,
        "compaction-manual-range-mismatch-rejected",
        "A requested compaction that starts after the first input.",
        log,
    )
    log = base()
    request = _request(log)
    user(log, "Go on.")
    _answered(log, request, "threshold")
    _rule(
        root,
        "compaction-manual-trigger-mismatch-rejected",
        "A compacted naming the request with trigger threshold.",
        log,
    )


def _breaker_open(root: pathlib.Path) -> None:
    log = base()
    for question in ("One?", "Two?", "Three?"):
        user(log, question)
        log.add("compaction_failed", {"stage": "summary", "reason": "still_over_threshold"})
        answer(log, "Noted.")
    request = _request(log)
    user(log, "Go on.")
    summary: Obj = {
        "content": [{"type": "text", "text": SUMMARY}],
        "stop_reason": "end_turn",
        "usage": tokens(900, 40),
    }
    cause = {"cause_event_id": request["event_id"]}
    appended: list[JsonValue] = [
        {"type": "model_request", "data": {"attempt": 1, "purpose": "compaction", **cause}},
        {"type": "model_response", "data": {"content": summary["content"]}},
        {
            "type": "compacted",
            "data": {
                "trigger": "manual",
                "from_seq": 2,
                "to_seq": num(request["seq"]) - 1,
                **cause,
            },
        },
        *TAIL,
    ]
    _recover(
        root,
        "compaction-manual-breaker-open",
        "Three failed compactions open the breaker, then Thread.compact records a request and "
        "the thread's next input is durable. The resumed run carries the request out anyway, "
        "before its turn request: one side request naming it, compacted{trigger: manual}, then "
        "the turn.",
        log,
        (appended, [summary, FINAL]),
    )


def _recorded_response(root: pathlib.Path) -> None:
    log = base()
    request = _request(log)
    user(log, "Go on.")
    summarize(log, request)
    cause = {"cause_event_id": request["event_id"]}
    compacted: Obj = {
        "type": "compacted",
        "actor_kind": "recovery",
        "data": {"trigger": "manual", "from_seq": 2, "to_seq": num(request["seq"]) - 1, **cause},
    }
    _recover(
        root,
        "compaction-manual-resume-recorded-response",
        "A crash after a requested compaction's summary was recorded and before its outcome. "
        "Recovery appends compacted from the recorded summary and never sends the summary "
        "request again; the side request didn't carry the turn's input, so the turn then "
        "continues with its own request.",
        log,
        ([compacted, *TAIL], [FINAL]),
    )


def _recover(
    root: pathlib.Path,
    name: str,
    desc: str,
    log: Log,
    steps: tuple[list[JsonValue], list[JsonValue]],
) -> None:
    appended, responses = steps
    write_case(
        root,
        case(name, FAM, "recover", desc, model_script="model.json"),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "appended": appended},
        extra={"model.json": {"responses": responses}},
    )
