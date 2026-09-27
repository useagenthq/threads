"""The A2A routes. The card is discovery and takes no version and no principal. On every other
route the order is fixed: version, then authenticate, then whether the agent is exposed, then the
tenant, then the operation — so an agent this host does not expose answers exactly as a path that
does not exist, and a caller cannot probe which agents we run.

The version and the caller both come from headers, so both are settled **before the body is read**:
an unauthenticated caller never gets us to parse a payload. The cost is that those two refusals
cannot echo a JSON-RPC id, which is only known once the body is parsed; a client that sent a bad
version or no credential has its answer either way."""

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import Response

from threads.a2a.protocol import Binding, check_version
from threads.host.a2a.card import card_bytes
from threads.host.a2a.config import Exposed
from threads.host.a2a.dispatch import run_operation
from threads.host.a2a.parse import Inbound, Named, operation, request_of, tenant_mismatch
from threads.host.a2a.wire import (
    Envelope,
    card_response,
    not_found,
    refuse,
    unauthenticated,
    version_of,
)
from threads.host.app import Host
from threads.log import Principal
from threads.result import Err


async def agent_card(host: Host, request: Request) -> Response:
    """GET /a2a/{agent}/.well-known/agent-card.json: discovery, so no version and no principal."""
    exposed = host.exposed
    name = request.path_params["agent"]
    agent = None if exposed is None else exposed.agents.get(name)
    if exposed is None or agent is None:
        return not_found("HTTP+JSON")
    return card_response(card_bytes(exposed, name, agent, exposed.base_url))


async def sole_card(host: Host, request: Request) -> Response:
    """GET /.well-known/agent-card.json, served only when exactly one agent is exposed: with two
    there is no answer that is not a guess. The alias is only where the card was found; the
    endpoints it names are the agent's own."""
    exposed = host.exposed
    only = () if exposed is None else tuple(exposed.agents)
    if exposed is None or len(only) != 1:
        return not_found("HTTP+JSON")
    name = only[0]
    return card_response(card_bytes(exposed, name, exposed.agents[name], exposed.base_url))


async def operation_route(host: Host, request: Request, binding: Binding) -> Response:
    """Every operation of both bindings: POST /a2a/{agent} is JSON-RPC, everything under it is
    HTTP+JSON."""
    exposed = host.exposed
    if exposed is None:
        return not_found(binding)
    path = "" if binding == "JSONRPC" else f"/{request.path_params['path']}"
    inbound = Inbound(request, request.path_params["agent"], path, binding)
    return await _dispatch(host, exposed, inbound)


async def _dispatch(host: Host, exposed: Exposed, inbound: Inbound) -> Response:
    """Version, then principal, and only then the body."""
    bare = Envelope(inbound.binding)
    header, query = version_of(inbound.request)
    wrong = check_version(header, query)
    if wrong is not None:
        return refuse(bare, wrong)
    who = await _principal(host, inbound.request)
    if who is None:
        return unauthenticated(bare)
    return await _operation(host, exposed, inbound, who)


async def _operation(host: Host, exposed: Exposed, inbound: Inbound, who: Principal) -> Response:
    """The agent, the tenant and then the operation: everything that needs the body read."""
    parsed = await operation(inbound)
    if not isinstance(parsed, Named):
        return refuse(parsed.envelope, parsed.fault)
    agent = exposed.agents.get(inbound.name)
    if agent is None:
        return not_found(inbound.binding)
    request = request_of(parsed.method, parsed.params)
    if isinstance(request, Err):
        return refuse(parsed.envelope, request.error)
    foreign = tenant_mismatch(request, who)
    if foreign is not None:
        return refuse(parsed.envelope, foreign)
    return await run_operation(
        host.runner, who, inbound, parsed.envelope, agent, parsed.method, parsed.params
    )


async def _principal(host: Host, request: Request) -> Principal | None:
    """`authenticate` is the app's own code, so its answer is parsed, never trusted as typed."""
    if host.authenticate is None:
        return None
    raw = await host.authenticate(request)
    if raw is None:
        return None
    try:
        return Principal.model_validate(raw)
    except ValidationError:
        return None
