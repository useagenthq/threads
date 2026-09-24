# pyright: strict
"""A recorded turn for the eval conformance cases (lane 22): the events the loop appends for
it, and the files saveCase writes from them (model.json, sandbox.json v2, stubs.json,
extensions.json, line0.json, the full `appended`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .common import ALICE, ALLOW, sha, tokens
from .jcs import JsonValue, Obj, canonical

if TYPE_CHECKING:
    from .log import Log

USAGE = tokens(10, 2)


class Turn:
    """Appends one turn to `log` as the loop would, and keeps what the case files need."""

    def __init__(self, log: Log) -> None:
        self.log = log
        self.restore = log.seq
        # The case log: the branch through the event before the turn's run.
        self.prefix = log.copy()
        self.responses: list[JsonValue] = []
        self.results: list[JsonValue] = []
        self.stubs: list[JsonValue] = []
        self.hooks: list[JsonValue] = []
        self.recall: list[JsonValue] = []
        self._seen: dict[tuple[str, str], int] = {}
        self._hook_seen: dict[tuple[str, str], int] = {}
        self.text = ""

    # ---- the turn ----
    def user(self, text: str) -> Obj:
        self.text = text
        return self.log.add(
            "user_input", {"source": "api", "text": text}, actor="user", principal=ALICE
        )

    def request(self) -> Obj:
        return self.log.model_request()

    def respond(self, req: Obj, content: list[JsonValue], stop: str) -> Obj:
        self.responses.append({"content": content, "stop_reason": stop, "usage": USAGE})
        return self.log.model_response(req, content, stop, USAGE)

    def uses(self, req: Obj, calls: list[tuple[str, str, Obj]], *, allow: bool = False) -> None:
        """A response that calls each (call_id, name, input), then each call's tool_call, and
        with `allow` each call's permission right after it, as the loop records several."""
        content: list[JsonValue] = [
            {"type": "tool_use", "call_id": c, "name": n, "input": i} for c, n, i in calls
        ]
        self.respond(req, content, "tool_use")
        for c, n, i in calls:
            self.log.tool_call(req, c, n, i)
            if allow:
                self.allow(c)

    def allow(self, call_id: str) -> None:
        self.log.add("permission_decision", {"call_id": call_id, **ALLOW, "mode": "default"})

    def decided(self, call_id: str, source: str) -> None:
        """An allow the policy didn't make: a hook's, or the mode's for a framework tool."""
        data: Obj = {"call_id": call_id, "decision": "allow", "source": source, "mode": "default"}
        self.log.add("permission_decision", data)

    def result(
        self,
        call_id: str,
        preview: str,
        *,
        is_error: bool = False,
        content: list[JsonValue] | None = None,
    ) -> None:
        data: Obj = {
            "call_id": call_id,
            "is_error": is_error,
            "completeness": "complete",
            "preview": preview,
            "origin": "executed",
        }
        if content is not None:
            data["content"] = content
        self.log.add("tool_result", data, actor="tool")

    def read(self, call_id: str, name: str, inp: Obj, output: str, **more: JsonValue) -> None:
        """A read-only call's permission and result, recorded for sandbox.json v2."""
        self.allow(call_id)
        content = more.get("content")
        listed = content if isinstance(content, list) else None
        self.result(call_id, output, content=listed)
        self.record_read(call_id, name, inp, output, listed)

    def record_read(
        self,
        call_id: str,
        name: str,
        inp: Obj,
        output: str,
        content: list[JsonValue] | None = None,
    ) -> None:
        """The read-only result sandbox.json v2 keeps: occurrences count from 1."""
        del call_id
        key = (name, sha(canonical(inp)))
        self._seen[key] = self._seen.get(key, 0) + 1
        record: Obj = {
            "tool": name,
            "args_hash": key[1],
            "occurrence": self._seen[key],
            "is_error": False,
            "preview": output,
        }
        if content is not None:
            record["content"] = content
        self.results.append(record)

    def effect(self, call_id: str, name: str, inp: Obj, output: str) -> None:
        """A mediated call: begun, committed and stubbed (stubs.json counts from 0)."""
        self.allow(call_id)
        self.log.add("effect_begin", {"call_id": call_id, "attempt": 1})
        ref = self.log.art(output.encode(), "text/plain")
        self.log.add("effect_commit", {"call_id": call_id, "result_ref": ref})
        self.result(call_id, output)
        h = sha(canonical(inp))
        taken = sum(
            1
            for s in self.stubs
            if isinstance(s, dict) and s["tool"] == name and s["args_hash"] == h
        )
        self.stubs.append(
            {"tool": name, "args_hash": h, "occurrence": taken, "output": output, "is_error": False}
        )

    def say(self, text: str) -> Obj:
        req = self.request()
        return self.respond(req, [{"type": "text", "text": text}], "end_turn")

    def done(self) -> None:
        self.log.add("turn_completed", {"reason": "end_turn"})

    # ---- hooks ----
    def hook(  # noqa: PLR0913, PLR0917 - one hook_decision's fields, as the loop records them
        self,
        ext: str,
        hook: str,
        decision: str,
        key: Obj | None = None,
        reason: str | None = None,
        at: Obj | None = None,
    ) -> Obj:
        data: Obj = {"extension": ext, "hook": hook, "decision": decision}
        if reason is not None:
            data["reason"] = reason
        data |= key or {}
        count = self._hook_seen.get((ext, hook), 0) + 1
        self._hook_seen[(ext, hook)] = count
        record: Obj = {"extension": ext, "hook": hook, "occurrence": count}
        if at is not None:
            record["at"] = at
        record["decision"] = decision
        if reason is not None:
            record["reason"] = reason
        record["injected"] = []
        self.hooks.append(record)
        return self.log.add("hook_decision", data)

    def inject(self, ext: str, text: str, trust: str = "untrusted_reference") -> None:
        data: Obj = {"source": "hook", "trust": trust, "origin": {"id": ext}, "text": text}
        last = self.hooks[-1]
        if isinstance(last, dict) and isinstance(last["injected"], list):
            last["injected"].append(data)
        self.log.add("injected", data)

    # ---- the case files ----
    def appended(self) -> list[JsonValue]:
        return [e for e in self.log.events if isinstance(e["seq"], int) and e["seq"] > self.restore]

    def files(self) -> dict[str, JsonValue | bytes]:
        out: dict[str, JsonValue | bytes] = {
            "model.json": {"responses": self.responses},
            "stubs.json": {"stubs": self.stubs},
        }
        if self.results:
            out["sandbox.json"] = {"version": 2, "results": self.results}
        if self.hooks or self.recall:
            out["extensions.json"] = {"hooks": self.hooks, "recall": self.recall}
        line0 = self.line0()
        if line0 is not None:
            out["line0.json"] = line0
        return out

    def line0(self) -> bytes | None:
        for e in self.appended():
            if isinstance(e, dict) and e["type"] == "model_request":
                data = e["data"]
                ref = data["request_ref"] if isinstance(data, dict) else None
                sha256 = ref["sha256"] if isinstance(ref, dict) else None
                body = self.log.artifacts[sha256] if isinstance(sha256, str) else b""
                return body.split(b"\n", 1)[0]
        return None
