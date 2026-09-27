"""A caller's provenance metadata is a claim and nothing more: it grants no authority, picks no
budget and never becomes the run's provenance principal, always the authenticated caller. The one
thing a claim can decide is a refusal that only hurts the liar: a hop count too deep. Mirrors
typescript/packages/host/test/a2a/claims.test.ts."""

import asyncio
from collections.abc import Callable, Coroutine

from a2a_agents import talker
from a2a_kit import ALICE, message, reaches, recorded_inputs, served, state_of, task, text_of
from pydantic import JsonValue
from pydantic.experimental.missing_sentinel import MISSING

from threadsai.a2a.protocol import PROVENANCE
from threadsai.log.keys import principal_key


def run(main: Callable[[], Coroutine[object, object, None]]) -> None:
    asyncio.run(main())


def _claiming(message_id: str, claim: JsonValue) -> JsonValue:
    return message(message_id, "hello", metadata={PROVENANCE: claim})


def test_a_claimed_hops_of_8_is_rejected_with_call_chain_too_deep_and_nothing_is_stored() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            refused = task(await on.rpc("SendMessage", _claiming("m1", {"hops": 8}), as_=ALICE))
            assert state_of(refused) == "TASK_STATE_REJECTED"
            assert text_of(refused) == "call chain too deep"
            # No run started, so a loop a liar induces costs us nothing at all.
            assert await recorded_inputs(on.store) == []

    run(main)


def test_a_deeper_claimed_hop_count_is_refused_too_and_a_shallower_one_runs() -> None:
    async def main() -> None:
        async with served({"support": talker("hi", "hi")}) as on:
            deep = task(await on.rpc("SendMessage", _claiming("deep", {"hops": 40}), as_=ALICE))
            assert state_of(deep) == "TASK_STATE_REJECTED"
            shallow = task(await on.rpc("SendMessage", _claiming("ok", {"hops": 7}), as_=ALICE))
            assert state_of(shallow) != "TASK_STATE_REJECTED"

    run(main)


def test_the_rejection_is_derived_so_a_retry_of_the_same_message_answers_identically() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            first = task(await on.rpc("SendMessage", _claiming("m1", {"hops": 9}), as_=ALICE))
            again = task(await on.rpc("SendMessage", _claiming("m1", {"hops": 9}), as_=ALICE))
            assert again["id"] == first["id"]
            assert state_of(again) == "TASK_STATE_REJECTED"

    run(main)


def test_a_claim_is_recorded_as_an_untrusted_claim_and_never_as_the_runs_principal() -> None:
    async def main() -> None:
        claim: JsonValue = {
            "hops": 2,
            "request_id": "partner-1",
            # A caller asserting it is somebody else must change nothing about who it is to us.
            "principal": {"issuer": "evil", "tenant": "acme", "subject": "root"},
        }
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", _claiming("m1", claim), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            recorded = await recorded_inputs(on.store)
            assert len(recorded) == 1
            arrived = recorded[0].data.a2a
            assert arrived is not MISSING
            # The claim is kept, verbatim, as a claim.
            assert arrived.claims == claim
            # And the actor is the authenticated caller, not the one the claim named.
            assert principal_key(recorded[0].actor.principal) == principal_key(ALICE)

    run(main)


def test_a_message_with_no_provenance_metadata_records_no_claim() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", message("m1", "hello"), as_=ALICE))
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            arrived = (await recorded_inputs(on.store))[0].data.a2a
            assert arrived is not MISSING
            assert arrived.claims is MISSING
            # The message and context are still recorded, which is what a retry is answered from.
            assert (arrived.message_id, arrived.context_id) == ("m1", sent["contextId"])

    run(main)


def test_metadata_that_is_not_an_object_is_ignored_rather_than_refused() -> None:
    async def main() -> None:
        async with served({"support": talker("hi")}) as on:
            sent = task(await on.rpc("SendMessage", _claiming("m1", "not an object"), as_=ALICE))
            assert state_of(sent) != "TASK_STATE_REJECTED"
            await reaches(on, ALICE, str(sent["id"]), ["TASK_STATE_COMPLETED"])
            assert (await recorded_inputs(on.store))[0].data.a2a.claims is MISSING  # type: ignore[union-attr] - the run recorded one

    run(main)
