# pyright: strict
"""Content parts: media, reasoning, hosted tools, unknown usage."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ADAPTER, ALICE, ALLOW, NOW, aref, tokens, tool
from .jcs import JsonValue, Obj, canonical
from .log import Log, reduce
from .pieces import (
    READ_FILE,
    call,
    case,
    negative,
    render_case,
    result,
    started,
    user,
    write_case,
)
from .policies import policy
from .projections import cost

if TYPE_CHECKING:
    import pathlib

PNG = b"\x89PNG\r\n\x1a\nfixture: login page screenshot"
SCREENSHOT = tool("screenshot", "Capture the sandbox desktop.", {}, "read_only")


def _image(log: Log, b: bytes = PNG) -> Obj:
    return {"type": "image_ref", "ref": log.art(b, "image/png"), "width": 1280, "height": 800}


def _user_parts(log: Log, parts: list[Obj]) -> Obj:
    return log.add(
        "user_input",
        {"source": "api", "content": list(parts)},
        actor="user",
        principal=ALICE,
    )


def build(root: pathlib.Path) -> None:
    fam = "tools_streaming"

    log = Log()
    started(log, [READ_FILE])
    _user_parts(log, [{"type": "text", "text": "What does this page show?"}, _image(log)])
    render_case(
        root,
        (
            "render-user-image-input",
            fam,
            "A user input with a text part and an image part. The image bytes are an artifact "
            "(image/png, width, height); Render v1 carries the part as recorded, in order, and "
            "the adapter sends the verified bytes.",
        ),
        log,
    )

    log = Log()
    started(log, [SCREENSHOT])
    user(log, "Take a screenshot of the login page.")
    call(log, "screenshot", {})
    parts: list[JsonValue] = [
        {"type": "text", "text": "Login page loaded."},
        _image(log),
        {"type": "text", "text": "Cursor at (640, 400)."},
    ]
    result(log, "call_1", "Screenshot 1280x800 of the login page.", content=list(parts))
    render_case(
        root,
        (
            "render-screenshot-tool-result",
            "sandboxes",
            "A computer-use screenshot result mixes text and an image. With content present the "
            "model sees exactly those ordered parts; preview is the plain-text rendering for "
            "logs and channels and is not rendered.",
        ),
        log,
    )

    log = Log()
    started(log, [READ_FILE])
    missing = aref(b"\x89PNG\r\n\x1a\nnever stored", "image/png")
    _user_parts(
        log,
        [
            {"type": "text", "text": "Describe this."},
            {"type": "image_ref", "ref": missing, "width": 1280, "height": 800},
        ],
    )
    write_case(
        root,
        case(
            "render-image-artifact-missing",
            fam,
            "render",
            "An image part references an artifact that is not in the store. Rendering verifies "
            "every artifact a rendered part references, so the request is never sent: "
            "artifact_missing at the event that carries the part.",
        ),
        log,
        {"outcome": "error", "error": {"code": "artifact_missing", "seq": log.seq}},
    )

    log = Log()
    started(log, [READ_FILE])
    use: Obj = {"type": "tool_use", "call_id": "call_1", "name": "read_file", "input": {}}
    _user_parts(log, [{"type": "text", "text": "run this"}, use])
    negative(
        root,
        "user-input-tool-use-part-rejected",
        "A user_input carries a tool_use part. InputPart excludes tool_use, so user content can "
        "never enter dispatch: the line fails its data schema (invalid_line).",
        log,
        ("invalid_line", log.seq),
    )

    _thinking(root)
    _hosted(root)
    _unknown_usage(root)


def _thinking(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE])
    user(log, "What is in README.md?")
    r = log.model_request()
    block = canonical(
        {"signature": "sig_0123", "thinking": "Read the file first.", "type": "thinking"}
    )
    thinking: Obj = {
        "type": "reasoning",
        "provider": "scripted",
        "model": "scripted-1",
        "format": "thinking",
        "ref": log.art(block, "application/json"),
    }
    use: Obj = {
        "type": "tool_use",
        "call_id": "call_1",
        "name": "read_file",
        "input": {"path": "README.md"},
    }
    log.model_response(r, [thinking, use], "tool_use", tokens(120, 40))
    log.tool_call(r, "call_1", "read_file", {"path": "README.md"})
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    result(log, "call_1", "# demo\n")
    render_case(
        root,
        (
            "render-thinking-block-replay",
            "log_fork_test",
            "A response with a signed thinking block and a tool_use. The block's exact provider "
            "JSON (with signature) is an artifact; the next request carries the reasoning part "
            "unchanged, by ref, and never as text. Replay re-renders byte for byte.",
        ),
        log,
    )


def _hosted(root: pathlib.Path) -> None:
    log = Log()
    adapter: Obj = {**ADAPTER, "settings": {"hosted_tools": ["web_search"]}}
    started(log, [], adapter=adapter)
    user(log, "When was Bun 1.3 released?")
    r = log.model_request()
    page = b"Bun 1.3 is released. Published 2026-01-10."
    blocks = canonical({"query": "bun 1.3 release", "results": ["https://bun.sh/blog/bun-v1.3"]})
    content: list[Obj] = [
        {
            "type": "hosted_tool",
            "provider": "scripted",
            "model": "scripted-1",
            "format": "server_tool",
            "name": "web_search",
            "ref": log.art(blocks, "application/json"),
        },
        {"type": "text", "text": "Bun 1.3 was released on 2026-01-10."},
        {
            "type": "citation",
            "source_kind": "web",
            "source_id": "https://bun.sh/blog/bun-v1.3",
            "title": "Bun 1.3",
            "ref": log.art(page, "text/plain"),
            "span": {"start": 0, "end": 20},
            "cited_text": "Bun 1.3 is released.",
        },
    ]
    log.model_response(r, list(content), "end_turn", tokens(300, 30))
    log.add("turn_completed", {"reason": "end_turn"})
    user(log, "Thanks.")
    render_case(
        root,
        (
            "render-hosted-search-citations",
            "tools_streaming",
            "A provider-hosted web search declared in line 0 adapter settings. Its call and "
            "results are one hosted_tool part (execution owner: provider, never a tool_call) "
            "followed by text and a citation part whose span is in UTF-8 bytes of the cited "
            "artifact. The next request carries all parts as recorded.",
        ),
        log,
    )


def _unknown_usage(root: pathlib.Path) -> None:
    log = Log()
    started(log, [READ_FILE], policy=policy())
    user(log, "What is in README.md?")
    r = log.model_request()
    inp: Obj = {"path": "README.md"}
    log.tool_call(r, "call_1", "read_file", inp)
    log.add("permission_decision", {"call_id": "call_1", **ALLOW})
    use: Obj = {"type": "tool_use", "call_id": "call_1", "name": "read_file", "input": inp}
    log.add(
        "model_response",
        {
            "request_event_id": r["event_id"],
            "content": [use],
            "stop_reason": "other",
            "usage": {"input_tokens": None, "output_tokens": None},
            "completeness": "partial",
        },
        actor="model",
    )
    result(log, "call_1", "# demo\n")
    r2 = log.model_request()
    log.model_response(r2, [{"type": "text", "text": "One heading."}], "end_turn", tokens(160, 12))
    log.add("turn_completed", {"reason": "end_turn"})
    write_case(
        root,
        case(
            "usage-unknown-after-stream-break",
            "cancellation_resume",
            "reduce",
            "The stream broke after a streamed tool call and before the usage report: the "
            "attempt is sealed as a partial response whose usage fields are null (unknown). "
            "reduce sums only known values and counts the unknown response; cost is incomplete "
            "and its upper bound charges request bytes for input and max_tokens for output.",
        ),
        log,
        {"outcome": "ok", "state": reduce(log, NOW), "projections": {"cost": cost(log)}},
    )
