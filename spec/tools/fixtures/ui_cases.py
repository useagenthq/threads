# pyright: strict
"""`ui` cases, one per mapping row and closing outcome (spec/schema/ui/README.md, "Mapping",
"Opening and closing"). Each is written once per protocol."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, tokens
from .log import Log
from .pieces import READ_FILE, started
from .policies import CONTEXT, policy
from .ui_pieces import (
    CHILD,
    answered,
    asked,
    both,
    called,
    decided,
    end,
    question,
    respond,
    result,
    say,
    ui_case,
    ui_log,
    use,
    user,
)

if TYPE_CHECKING:
    import pathlib

    from .jcs import Obj

CH = "0192c000-0000-7000-8000-0000000000a1"


def build(root: pathlib.Path) -> None:
    _text_and_tool(root)
    _approvals(root)
    _questions(root)
    _results(root)
    _children(root)
    _model_rows(root)
    _closing(root)


def run_of(log: Log) -> Obj:
    return {"run_id": next(e for e in log.events if e["type"] == "user_input")["event_id"]}


def _text_and_tool(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "What is in README.md?")
    inp: Obj = {"path": "README.md"}
    r, _ = respond(log, [say("Let me look."), use("call_1", "read_file", inp)], "tool_use")
    called(log, r, use("call_1", "read_file", inp))
    result(log, "call_1", "# demo\n")
    respond(log, [say("One heading: demo.")])
    end(log)
    both(
        root,
        "ui-text-and-tool",
        "Two steps: text before a tool call in one response, its result, then a final text. "
        "Text parts are id'd <request event_id>:<index>; the call is a tool part (AI SDK) or "
        "joins its request's assistant message (AG-UI).",
        log,
        run_of(log),
    )


def _approvals(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Clean the build folder.")
    binding = asked(log, "call_1", CH, "rm -rf build")
    both(
        root,
        "ui-approval-park",
        "A call parked on approval: the AI SDK gets tool-approval-request and finish{tool-calls}; "
        "AG-UI gets RUN_FINISHED{interrupt} with the challenge's id, schema and expiry.",
        log,
        run_of(log),
    )
    grant = decided(log, binding, grant=True)
    result(log, "call_1", "removed")
    respond(log, [say("Cleaned.")])
    end(log)
    run = run_of(log)
    for p, extra in (
        ("ai-sdk", {"after": f"{grant['seq']}:0"}),
        ("ag-ui", {"after": f"{int(str(grant['seq'])) - 1}:0"}),
    ):
        ui_case(
            root,
            f"ui-approval-resumed-{p}",
            "The grant, the resumed tool result and the final text, from the cursor after the "
            "grant (AI SDK), and from the park's event boundary as a snapshot replay (AG-UI).",
            log,
            {"protocol": p, **run, **extra},
        )
    log = ui_log()
    user(log, "Clean the build folder.")
    binding = asked(log, "call_1", CH, "rm -rf build")
    decided(log, binding, grant=False, reason="not today")
    result(log, "call_1", "denied: not today", is_error=True, origin="denied")
    respond(log, [say("I left it alone.")])
    end(log)
    both(
        root,
        "ui-denied",
        "A denied approval: tool-approval-response{approved: false, reason} then "
        "tool-output-denied (AI SDK); a TOOL_CALL_RESULT with the preview (AG-UI).",
        log,
        run_of(log),
    )


def _questions(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Deploy it.")
    question(log, "call_1", "Staging or production?")
    both(
        root,
        "ui-question-park",
        "An ask_user park: a tool-ask_user part ending finish{tool-calls} (AI SDK); an "
        "interrupt of reason user_input with the question and the Answer schema (AG-UI).",
        log,
        run_of(log),
    )
    answered(log, "call_1", "Staging.")
    respond(log, [say("Deployed to staging.")])
    end(log)
    both(
        root,
        "ui-question",
        "An ask_user park and its answer: the answer is the tool's output, then the run goes on.",
        log,
        run_of(log),
    )


def _results(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Read secrets.txt.")
    inp: Obj = {"path": "secrets.txt"}
    r, _ = respond(log, [use("call_1", "read_file", inp)], "tool_use")
    called(log, r, use("call_1", "read_file", inp))
    result(log, "call_1", "no such file", is_error=True)
    respond(log, [say("It is not there.")])
    end(log)
    both(
        root,
        "ui-tool-error",
        "A tool error: tool-output-error{errorText: preview} (AI SDK), TOOL_CALL_RESULT (AG-UI).",
        log,
        run_of(log),
    )
    log = ui_log()
    user(log, "Scan the dependencies in the background.")
    inp = {"agent": "scanner", "prompt": "Scan deps."}
    r, _ = respond(log, [use("call_1", "spawn_agent", inp)], "tool_use")
    called(log, r, use("call_1", "spawn_agent", inp))
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": CHILD,
        "agent_name": "scanner",
        "mode": "background",
        "isolation": "shared_sandbox",
    }
    log.add("agent_spawned", spawned)
    result(log, "call_1", "scanner started", origin="deferred")
    respond(log, [say("The scan is running.")])
    end(log)
    finished: Obj = {"child_thread_id": CHILD, "status": "completed", "usage": tokens(10, 2)}
    log.add("agent_finished", finished)
    late: Obj = {
        "call_id": "call_1",
        "is_error": False,
        "completeness": "complete",
        "preview": "No vulnerable dependencies.",
    }
    log.add("tool_result_late", late)
    both(
        root,
        "ui-deferred-then-late",
        "A background child: its call's deferred placeholder is a preliminary output (AI SDK; "
        "nothing on AG-UI), the child a data-subagent part / SUBAGENT_STARTED..FINISHED, and "
        "the late result the call's output.",
        log,
        run_of(log),
    )


def _children(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Review the diff.")
    inp: Obj = {"agent": "reviewer", "prompt": "Review the diff."}
    r, _ = respond(log, [use("call_1", "spawn_agent", inp)], "tool_use")
    called(log, r, use("call_1", "spawn_agent", inp))
    spawned: Obj = {
        "call_id": "call_1",
        "child_thread_id": CHILD,
        "agent_name": "reviewer",
        "mode": "foreground",
        "isolation": "shared_sandbox",
    }
    log.add("agent_spawned", spawned)
    log.add("agent_finished", {"child_thread_id": CHILD, "status": "failed", "usage": tokens(5, 1)})
    result(log, "call_1", "reviewer failed", is_error=True)
    respond(log, [say("The review failed.")])
    end(log)
    both(
        root,
        "ui-legacy-subagent",
        "A foreground child that fails: data-subagent{status: failed} (AI SDK); "
        "SUBAGENT_ERROR{message: 'subagent reviewer ended: failed', code: failed} (AG-UI).",
        log,
        run_of(log),
    )


def _model_rows(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Think, then answer.")
    thought = log.art(b'{"type":"thinking"}', "application/json")
    reasoning: Obj = {
        "type": "reasoning",
        "provider": "scripted",
        "model": "scripted-1",
        "format": "thinking",
        "ref": thought,
        "summary": "Weigh both options.",
    }
    respond(log, [reasoning, say("Option A.")])
    end(log)
    both(
        root,
        "ui-reasoning-summary",
        "A reasoning part with a summary streams the summary as a reasoning part (AI SDK) or a "
        "reasoning message (AG-UI); its provider bytes never show.",
        log,
        run_of(log),
    )
    for name, reason, desc in (
        (
            "ui-retry-wait",
            "overloaded",
            "A rejected attempt then a scheduled retry: the abandoned step closes, and the wait "
            "is data-status{retry_wait} / CUSTOM threads.retry_wait with not_before.",
        ),
        (
            "ui-attempt-abandoned",
            "stream_broken",
            "An abandoned attempt closes its step with data-attempt / "
            "CUSTOM threads.attempt_abandoned; the resent attempt's parts use its new request id.",
        ),
    ):
        log = ui_log()
        user(log, "Say hi.")
        r = log.model_request()
        outcome = "failed" if reason == "overloaded" else "unknown"
        gone = log.add(
            "model_attempt_abandoned",
            {"request_event_id": r["event_id"], "provider_outcome": outcome, "reason": reason},
        )
        if reason == "overloaded":
            wait: Obj = {
                "request_event_id": r["event_id"],
                "delay_ms": 1000,
                "not_before": int(str(gone["time"])) + 1000,
                "basis": "backoff",
            }
            log.add("retry_scheduled", wait)
        r2 = log.model_request(2)
        log.model_response(r2, [say("Hi.")], "end_turn", tokens(10, 2))
        end(log)
        both(root, name, desc, log, run_of(log))
    log = ui_log()
    user(log, "Say something rude.")
    respond(log, [say("I can't help with that.")], "refusal")
    end(log)
    both(
        root,
        "ui-refusal-content-filter",
        "A response that stopped with refusal completes the run with finish{content-filter} "
        "(AI SDK) and RUN_FINISHED success (AG-UI).",
        log,
        run_of(log),
    )
    _compaction(root)


def _compaction(root: pathlib.Path) -> None:
    log = _context_log()
    first = user(log, "What is in README.md?")
    respond(log, [say("One heading: demo.")])
    last = log.events[-1]
    end(log)
    run = user(log, "Go on.")
    side = log.model_request(compaction=True)
    log.model_response(side, [say("They asked about README.md.")], "end_turn", tokens(900, 40))
    log.add(
        "compacted",
        {
            "from_seq": first["seq"],
            "to_seq": last["seq"],
            "from_event_id": first["event_id"],
            "to_event_id": last["event_id"],
            "summary_ref": log.art(b"They asked about README.md.", "text/plain"),
            "summary_request_event_id": side["event_id"],
            "trigger": "threshold",
        },
    )
    respond(log, [say("Continuing.")])
    end(log)
    both(
        root,
        "ui-compaction-invisible",
        "A compaction side request and its answer inside a run map to no frames.",
        log,
        {"run_id": run["event_id"]},
    )


def _context_log() -> Log:
    """A log whose pinned policy allows compaction."""
    log = Log()
    started(log, [READ_FILE], policy=policy(context=CONTEXT))
    return log


def _closing(root: pathlib.Path) -> None:
    log = ui_log()
    user(log, "Clean up.")
    inp: Obj = {"path": "big.log"}
    r, _ = respond(log, [say("Reading."), use("call_1", "read_file", inp)], "tool_use")
    called(log, r, use("call_1", "read_file", inp))
    cr = log.add("cancel_requested", {"scope": "thread"}, actor="user", principal=ALICE)
    result(log, "call_1", "not executed: cancelled", is_error=True, origin="not_executed")
    log.add("cancelled", {"request_event_id": cr["event_id"]})
    end(log, "cancelled")
    both(
        root, "ui-cancelled", "A cancelled run: abort / RUN_FINISHED{cancelled}.", log, run_of(log)
    )
    log = ui_log()
    user(log, "Read everything.")
    exceeded: Obj = {
        "scope": "run",
        "limit": "max_model_requests",
        "limit_value": 0,
        "observed": 1,
        "observed_is_upper_bound": False,
    }
    log.add("budget_exceeded", exceeded)
    end(log, "budget_exhausted")
    both(
        root,
        "ui-budget-exhausted",
        "A run out of budget: error{'budget_exhausted: <limit>'} then finish{error} / RUN_ERROR.",
        log,
        run_of(log),
    )
    log = ui_log()
    user(log, "Say hi.")
    r = log.model_request()
    data: Obj = {
        "request_event_id": r["event_id"],
        "provider_outcome": "failed",
        "reason": "overloaded",
    }
    log.add("model_attempt_abandoned", data)
    end(log, "model_unavailable")
    both(
        root,
        "ui-model-unavailable",
        "No model could be reached: error{'failed: model_unavailable'} / RUN_ERROR{code: failed}.",
        log,
        run_of(log),
    )
