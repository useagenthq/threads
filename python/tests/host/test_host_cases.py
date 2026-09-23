"""Conformance runner for `host` cases (spec/conformance/README.md, ): each
request goes to POST /v1/runs of a host whose only agent is `demo`, as the principal the case
names, and the answers and the recorded user_inputs are compared."""

import asyncio
import json
from http import HTTPStatus

import httpx
import pytest
from corpus import CASES, cases, load
from pydantic import JsonValue
from starlette.requests import Request

from threads import agent, scripted_model, sqlite
from threads.agents.store import open_store, scoped
from threads.host import host
from threads.log import BranchId, Principal, UserInputEvent
from threads.result import Ok

DONE: JsonValue = {
    "content": [{"type": "text", "text": "Done."}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}
AS: str = "x-conformance-principal"


async def as_named(request: Request) -> Principal | None:
    """The case's principal for this request: the runner's stand-in for real authentication."""
    named = request.headers.get(AS)
    return None if named is None else Principal.model_validate_json(named)


@pytest.mark.parametrize("name", cases("host"))
def test_host_case(name: str) -> None:
    case, expected = load(CASES / name, "case.json"), load(CASES / name, "expected.json")
    given = case["input"]
    assert isinstance(given, dict)
    requests = given["requests"]
    assert isinstance(requests, list)
    demo = agent(name="demo", model=scripted_model({"responses": [DONE] * (len(requests) + 1)}))
    store = sqlite(":memory:")

    async def main() -> tuple[list[JsonValue], int]:
        served = host(store=store, agents={"demo": demo}, authenticate=as_named)
        answers: list[JsonValue] = []
        receipts: list[tuple[int, JsonValue]] = []
        named: dict[tuple[str, str], str] = {}
        async with served:
            transport = httpx.ASGITransport(app=served.asgi)
            async with httpx.AsyncClient(transport=transport, base_url="http://host") as client:
                for index, request in enumerate(requests):
                    assert isinstance(request, dict)
                    headers = {
                        AS: json.dumps(request["principal"]),
                        "idempotency-key": str(request["idempotency_key"]),
                    }
                    response = await client.post("/v1/runs", json=request["body"], headers=headers)
                    answers.append(_answer(response, index, receipts))
                    body = response.json()
                    principal = request["principal"]
                    assert isinstance(principal, dict)
                    if response.status_code == HTTPStatus.ACCEPTED:
                        named[(str(principal["tenant"]), body["thread_id"])] = body["branch_id"]
        return answers, await _user_inputs(named)

    async def _user_inputs(named: dict[tuple[str, str], str]) -> int:
        total = 0
        for (tenant, _thread), branch in named.items():
            sq = await open_store(scoped(store, tenant))
            read = await sq.read(BranchId(branch), 0)
            assert isinstance(read, Ok)
            total += sum(isinstance(e, UserInputEvent) for e in read.value.fold.events)
        return total

    answers, inputs = asyncio.run(main())
    assert answers == expected["api"]
    assert inputs == expected["user_inputs"]


def _answer(
    response: httpx.Response, index: int, receipts: list[tuple[int, JsonValue]]
) -> JsonValue:
    """status, the index of the first request whose receipt this 202 equals, or the error code.
    A failure body is exactly an ErrorBody: it never carries another request's receipt."""
    body = response.json()
    if response.status_code != HTTPStatus.ACCEPTED:
        assert set(body) == {"error"}
        return {"status": response.status_code, "code": body["error"]["code"]}
    receipts.append((index, body))
    first = next(i for i, seen in receipts if seen == body)
    return {"status": response.status_code, "receipt": first}
