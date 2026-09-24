"""Shared by the UI route tests: a served host of the http tests' `sender` agent, and the frames
of an SSE body."""

import json
from collections.abc import Mapping

import httpx
from host.test_http import as_
from pydantic import JsonValue

AI_SDK = "/v1/ui/ai-sdk/support"
AG_UI = "/v1/ui/ag-ui/support"


def frames(response: httpx.Response) -> list[tuple[str | None, JsonValue]]:
    """(id, data) per SSE message; AI SDK's final [DONE] is ("[DONE]" data, None id)."""
    out: list[tuple[str | None, JsonValue]] = []
    for block in response.text.strip().split("\n\n"):
        if not block:
            continue
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        data = fields["data"]
        out.append((fields.get("id"), data if data == "[DONE]" else json.loads(data)))
    return out


def types(response: httpx.Response) -> list[str]:
    return [str(d.get("type")) if isinstance(d, dict) else str(d) for _, d in frames(response)]


def user(message_id: str, text: str) -> JsonValue:
    return {"id": message_id, "role": "user", "parts": [{"type": "text", "text": text}]}


def chat(key: str, *messages: JsonValue) -> dict[str, JsonValue]:
    return {"id": key, "messages": list(messages), "trigger": "submit-message"}


async def post(
    client: httpx.AsyncClient, path: str, who: str, body: Mapping[str, JsonValue]
) -> httpx.Response:
    return await client.post(path, json=body, headers=as_(who))
