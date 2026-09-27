"""The routes, served: the order every operation is decided in, and the cards. The order is not
negotiable — **version, then principal, settled before the body is read** — so an unauthenticated
caller never gets us to parse a payload, and an agent this host does not expose answers exactly as a
path that does not exist. Mirrors typescript/packages/host/test/a2a/protocol.test.ts and the route
half of card.test.ts."""

import asyncio
import json
from collections.abc import Callable, Coroutine
from http import HTTPStatus
from typing import Final

import pytest
from a2a_agents import talker
from a2a_kit import ALICE, ONE, Agents, fault_name, message, served, task

from threadsai.a2a.protocol import A2A_JSON, VERSION_HEADER
from threadsai.host.a2a.config import A2aOptions

EVE: Final = "01a00000-0000-7000-8000-000000000000"
TWO: Final[A2aOptions] = {
    "base_url": "https://host.test",
    "expose": {
        "support": {"description": "Support."},
        "billing": {"description": "Billing."},
    },
}


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _bot() -> Agents:
    return {"support": talker("hi", "hi")}


class TestTheVersion:
    def test_no_version_in_header_or_query_is_refused_never_a_default_to_1_0(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                bare = await on.rpc("GetTask", {"id": EVE}, as_=ALICE, version=None)
                assert fault_name(bare) == "VersionNotSupportedError"

        run(main)

    @pytest.mark.parametrize("version", ["0.3", "2.0", "nonsense"])
    def test_a_version_we_do_not_speak_is_refused(self, version: str) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                wrong = await on.rpc("GetTask", {"id": EVE}, as_=ALICE, version=version)
                assert fault_name(wrong) == "VersionNotSupportedError"

        run(main)

    def test_the_version_is_compared_as_major_minor_so_1_0_1_is_1_0(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                fine = await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE, version="1.0.1")
                assert isinstance(task(fine)["id"], str)

        run(main)

    def test_the_version_may_arrive_as_the_query_parameter_instead_of_the_header(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                answered = await on.raw(
                    "GET",
                    f"/a2a/support/tasks/{EVE}?{VERSION_HEADER}=1.0",
                    as_=ALICE,
                    version=None,
                )
                # It got as far as the operation, so the version was accepted.
                assert fault_name(answered) == "TaskNotFoundError"

        run(main)


class TestTheCaller:
    def test_an_unauthenticated_request_is_401_with_a_bearer_challenge(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                for answered in (
                    await on.rpc("GetTask", {"id": EVE}),
                    await on.http("GET", f"/tasks/{EVE}"),
                ):
                    assert answered.status_code == HTTPStatus.UNAUTHORIZED
                    assert answered.headers["www-authenticate"] == "Bearer"
                    assert fault_name(answered) == "InvalidRequestError"

        run(main)

    def test_an_unauthenticated_caller_never_gets_its_body_parsed(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                # A body that is not JSON at all: a 401 rather than a parse error proves the
                # credential was settled before the payload was read.
                answered = await on.raw("POST", "/a2a/support", raw_body="{not json")
                assert answered.status_code == HTTPStatus.UNAUTHORIZED
                assert fault_name(answered) == "InvalidRequestError"

        run(main)

    def test_a_version_we_do_not_speak_is_refused_before_the_body_is_read(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                answered = await on.raw(
                    "POST", "/a2a/support", as_=ALICE, raw_body="{not json", version="0.3"
                )
                assert fault_name(answered) == "VersionNotSupportedError"

        run(main)

    def test_a_host_with_no_authenticate_answers_401_on_every_principal_route(self) -> None:
        async def main() -> None:
            async with served(_bot(), with_auth=False) as on:
                answered = await on.rpc("GetTask", {"id": EVE}, as_=ALICE)
                assert answered.status_code == HTTPStatus.UNAUTHORIZED

        run(main)

    def test_a_tenant_that_is_not_the_callers_own_is_invalid_params(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                foreign = await on.rpc("GetTask", {"id": EVE, "tenant": "other"}, as_=ALICE)
                assert fault_name(foreign) == "InvalidParamsError"
                mine = await on.rpc("GetTask", {"id": EVE, "tenant": ALICE.tenant}, as_=ALICE)
                assert fault_name(mine) == "TaskNotFoundError"

        run(main)


class TestWhatMustLookMissing:
    def test_an_agent_that_is_not_exposed_is_404_exactly_as_a_path_that_does_not_exist(
        self,
    ) -> None:
        async def main() -> None:
            async with served({"support": talker("hi"), "secret": talker("hi")}) as on:
                hidden = await on.raw(
                    "POST",
                    "/a2a/secret",
                    as_=ALICE,
                    body={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": EVE}},
                )
                absent = await on.raw(
                    "POST",
                    "/a2a/nobody",
                    as_=ALICE,
                    body={"jsonrpc": "2.0", "id": 1, "method": "GetTask", "params": {"id": EVE}},
                )
                assert fault_name(hidden) == "MethodNotFoundError"
                assert hidden.text == absent.text

        run(main)

    def test_a_tenant_path_variant_of_an_http_json_operation_is_404(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                answered = await on.http("GET", f"/tenants/acme/tasks/{EVE}", as_=ALICE)
                assert fault_name(answered) == "MethodNotFoundError"

        run(main)

    def test_an_unmatched_path_under_a2a_is_method_not_found(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                answered = await on.http("GET", "/nothing/here", as_=ALICE)
                assert fault_name(answered) == "MethodNotFoundError"

        run(main)

    def test_the_a2a_routes_serve_nothing_on_a_host_without_the_option(self) -> None:
        async def main() -> None:
            from threadsai import sqlite  # noqa: PLC0415 - one test builds a host by hand
            from threadsai.host import host  # noqa: PLC0415 - one test builds a host by hand

            plain = host(store=sqlite(":memory:"), agents={"support": talker("hi")})
            async with plain:
                import httpx  # noqa: PLC0415 - one test needs its own client

                transport = httpx.ASGITransport(app=plain.asgi)
                async with httpx.AsyncClient(transport=transport, base_url="https://h.test") as c:
                    card = await c.get("/.well-known/agent-card.json")
                    assert card.status_code == HTTPStatus.NOT_FOUND

        run(main)


class TestTheCards:
    def test_the_card_is_readable_with_no_authenticate_and_no_credential(self) -> None:
        async def main() -> None:
            async with served(_bot(), with_auth=False) as on:
                card = await on.raw("GET", "/a2a/support/.well-known/agent-card.json", version=None)
                assert card.status_code == HTTPStatus.OK
                assert card.headers["content-type"].startswith(A2A_JSON)
                body = json.loads(card.text)
                assert isinstance(body, dict)
                assert body["name"] == "support"

        run(main)

    def test_the_interface_urls_are_the_configured_base_url_not_the_requests_origin(self) -> None:
        # A request's own URL comes from the request line and the Host header, both of which a
        # caller writes. Behind a reverse proxy - the normal deployment - a card derived from the
        # request advertises the internal origin and no partner can reach us; pointed at an attacker
        # it is an origin a caller substitutes into the one document whose job is to say where work
        # goes.
        async def main() -> None:
            elsewhere: A2aOptions = {
                "base_url": "https://agents.acme.example",
                "expose": {"support": {"description": "Support."}},
            }
            async with served(_bot(), elsewhere) as on:
                # The request itself goes to host.test, as every request in this kit does.
                card = await on.raw("GET", "/a2a/support/.well-known/agent-card.json", version=None)
                assert "https://agents.acme.example/a2a/support" in card.text
                assert "host.test" not in card.text
                alias = await on.raw("GET", "/.well-known/agent-card.json", version=None)
                assert "https://agents.acme.example/a2a/support" in alias.text

        run(main)

    def test_the_single_agent_alias_serves_the_one_exposed_agents_card(self) -> None:
        async def main() -> None:
            async with served(_bot(), ONE) as on:
                alias = await on.raw("GET", "/.well-known/agent-card.json", version=None)
                direct = await on.raw(
                    "GET", "/a2a/support/.well-known/agent-card.json", version=None
                )
                assert alias.status_code == HTTPStatus.OK
                # The alias is only where the card was found; the endpoints it names are the same.
                assert alias.text == direct.text

        run(main)

    def test_the_alias_is_not_served_when_two_agents_are_exposed(self) -> None:
        async def main() -> None:
            agents = {"support": talker("hi"), "billing": talker("hi")}
            async with served(agents, TWO) as on:
                alias = await on.raw("GET", "/.well-known/agent-card.json", version=None)
                assert alias.status_code == HTTPStatus.NOT_FOUND

        run(main)

    def test_a_card_for_an_agent_that_is_not_exposed_is_404(self) -> None:
        async def main() -> None:
            async with served({"support": talker("hi"), "secret": talker("hi")}) as on:
                card = await on.raw("GET", "/a2a/secret/.well-known/agent-card.json", version=None)
                assert card.status_code == HTTPStatus.NOT_FOUND
                assert fault_name(card) == "MethodNotFoundError"

        run(main)


class TestBothBindings:
    def test_both_answer_the_same_task_for_the_same_message(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                over_rpc = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
                over_http = task(
                    await on.http("POST", "/message:send", as_=ALICE, body=message("m1", "hi"))
                )
                assert over_http == over_rpc

        run(main)

    def test_an_http_json_answer_is_a2a_json_and_a_json_rpc_answer_is_application_json(
        self,
    ) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                over_rpc = await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE)
                over_http = await on.http(
                    "POST", "/message:send", as_=ALICE, body=message("m2", "hi")
                )
                assert over_rpc.headers["content-type"].startswith("application/json")
                assert over_http.headers["content-type"].startswith(A2A_JSON)

        run(main)

    def test_another_tenants_principal_gets_its_own_task_never_this_tenants(self) -> None:
        async def main() -> None:
            from threadsai.log import Principal  # noqa: PLC0415 - one test needs a second tenant

            eve = Principal(issuer="partner", tenant="other", subject="eve.partner.example")
            async with served(_bot()) as on:
                mine = task(await on.rpc("SendMessage", message("m1", "hi"), as_=ALICE))
                theirs = await on.rpc("GetTask", {"id": mine["id"]}, as_=eve)
                assert fault_name(theirs) == "TaskNotFoundError"

        run(main)


class TestTheOperationsWeDoNotServe:
    """The cuts, over the served routes: each answers by name because the card says we do not serve
    it, so a client that read `pushNotifications: false` and called anyway gets the specific
    refusal rather than a 404 that looks like a wrong path."""

    def test_a_push_notification_config_path_says_so_by_name(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                for verb, path in (
                    ("GET", f"/tasks/{EVE}/pushNotificationConfigs"),
                    ("POST", f"/tasks/{EVE}/pushNotificationConfigs"),
                    ("DELETE", f"/tasks/{EVE}/pushNotificationConfigs/c1"),
                ):
                    answered = await on.http(verb, path, as_=ALICE)
                    assert fault_name(answered) == "PushNotificationNotSupportedError", path

        run(main)

    def test_the_extended_card_says_so_by_name(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                answered = await on.http("GET", "/extendedAgentCard", as_=ALICE)
                assert fault_name(answered) == "UnsupportedOperationError"

        run(main)

    def test_a_send_that_asks_for_push_notifications_is_refused_by_name(self) -> None:
        async def main() -> None:
            async with served(_bot()) as on:
                body = message("m1", "hi")
                assert isinstance(body, dict)
                asked = dict(body)
                asked["configuration"] = {"taskPushNotificationConfig": {"url": "x"}}
                answered = await on.rpc("SendMessage", asked, as_=ALICE)
                assert fault_name(answered) == "PushNotificationNotSupportedError"

        run(main)
