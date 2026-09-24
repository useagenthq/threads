# pyright: strict
"""The expected turn, model and tool spans: their attributes from the events that open and
close them (spec/otel/README.md, "Attributes"). Which events those are is the case's choice."""

from __future__ import annotations

from .common import arr, num, obj, text
from .jcs import Obj, canonical
from .otel_expect import (
    CLIENT,
    INTERNAL,
    OK_REASONS,
    PROVIDERS,
    Attr,
    Ctx,
    Shape,
    Span,
    span,
)


def turn(
    ctx: Ctx,
    shape: Shape,
    ends: tuple[Obj, Obj],
    run_id: str | None,
    parent_missing: bool = False,
) -> Span:
    close = ends[1]
    attrs: dict[str, Attr] = {
        "gen_ai.operation.name": "invoke_agent",
        "gen_ai.agent.name": ctx.agent,
        "gen_ai.conversation.id": text(close["thread_id"]),
        "threads.run_id": run_id,
        "threads.parent_missing": parent_missing,
    }
    status = None
    if close["type"] == "parked":
        attrs["threads.parked"] = True
    else:
        reason = text(obj(close["data"])["reason"])
        attrs["threads.turn.reason"] = reason
        status = None if reason in OK_REASONS else reason
    return span(ctx, shape, f"invoke_agent {ctx.agent}", INTERNAL, ends, attrs, status)


def _usage(u: Obj) -> dict[str, Attr]:
    parts = [u.get("input_tokens"), u.get("cache_read_tokens", 0), u.get("cache_write_tokens", 0)]
    total = None if any(p is None for p in parts) else sum(num(p) for p in parts if p is not None)
    out: dict[str, Attr] = {"gen_ai.usage.input_tokens": total}
    for key, name in (
        ("cache_read_tokens", "gen_ai.usage.cache_read.input_tokens"),
        ("cache_write_tokens", "gen_ai.usage.cache_creation.input_tokens"),
        ("output_tokens", "gen_ai.usage.output_tokens"),
        ("reasoning_tokens", "gen_ai.usage.reasoning.output_tokens"),
    ):
        v = u.get(key)
        out[name] = None if v is None else num(v)
    return out


def chat(ctx: Ctx, shape: Shape, ends: tuple[Obj, Obj], model: Obj | None = None) -> Span:
    """A model call closed by its response, its abandonment or (cut) the turn's close."""
    req, close = ends
    m = model or ctx.model
    d = obj(req["data"])
    provider = text(m["provider"])
    attrs: dict[str, Attr] = {
        "gen_ai.operation.name": "chat",
        "gen_ai.provider.name": PROVIDERS.get(provider, provider),
        "gen_ai.request.model": text(m["name"]),
        "threads.model.attempt": num(d["attempt"]),
        "threads.model.purpose": text(d.get("purpose", "turn")),
    }
    status = None
    c = obj(close["data"])
    if close["type"] in ("model_response", "model_response_recovered"):
        attrs["gen_ai.response.finish_reasons"] = [text(c["stop_reason"])]
        attrs |= _usage(obj(c["usage"]))
        if ctx.content:
            parts = [obj(p) for p in arr(c["content"])]
            attrs["threads.model.output_text"] = [
                text(p["text"]) for p in parts if p["type"] == "text"
            ]
    elif close["type"] == "model_attempt_abandoned":
        status = text(c["reason"])
        attrs["error.type"] = status
    else:
        status, attrs["threads.cut"] = "cut", True
    return span(ctx, shape, f"chat {m['name']}", CLIENT, ends, attrs, status)


def call_span(  # noqa: PLR0913, PLR0917 - one span's parts
    ctx: Ctx,
    shape: Shape,
    call: Obj,
    ends: tuple[Obj, Obj],
    effect_class: str | None,
    resumed: bool = False,
) -> Span:
    """A tool span (opened at `call`) or a continuation (opened at a `resumed`), closed by the
    call's tool_result, a park, or (cut) another turn close."""
    d = obj(call["data"])
    close = ends[1]
    attrs: dict[str, Attr] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": text(d["name"]),
        "gen_ai.tool.call.id": text(d["call_id"]),
        "gen_ai.tool.type": "function",
        "threads.tool.effect_class": effect_class,
        "threads.resumed": resumed,
    }
    if ctx.content:
        attrs["gen_ai.tool.call.arguments"] = canonical(d["input"]).decode()
    status = None
    if close["type"] == "tool_result":
        c = obj(close["data"])
        if ctx.content:
            attrs["gen_ai.tool.call.result"] = text(c["preview"])
        if c["is_error"] is True:
            status = text(c["origin"])
    elif close["type"] == "parked":
        attrs["threads.parked"] = True
    else:
        status, attrs["threads.cut"] = "cut", True
    return span(ctx, shape, f"execute_tool {d['name']}", INTERNAL, ends, attrs, status)
