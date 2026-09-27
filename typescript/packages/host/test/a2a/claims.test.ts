import { afterEach, expect, test } from "bun:test";
import { PROVENANCE } from "@threadsai/a2a/protocol";
import { principalKey, storeConnection } from "threadsai/host";
import { z } from "zod";
import { alice, knownEventsOf } from "../kit";
import { sqlAll } from "../sql";
import { talker } from "./agents";
import { message, reaches, serve, stopAll, task } from "./kit";

// A caller's provenance metadata is a claim and nothing more: it grants no authority, picks no
// budget and never becomes the run's provenance principal, which is always the authenticated caller.
// The one thing a claim can decide is a refusal that only hurts the liar: a hop count too deep.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

function claiming(id: string, claim: unknown): unknown {
  return message(id, "hello", { metadata: { [PROVENANCE]: claim } });
}

/** Every user_input in the store, with its actor and its recorded a2a claim. */
async function inputs(
  on: Awaited<ReturnType<typeof serve>>,
): Promise<
  readonly Extract<
    Awaited<ReturnType<typeof knownEventsOf>>[number],
    { type: "user_input" }
  >[]
> {
  const { db } = await storeConnection(on.store);
  const branches = z
    .array(z.strictObject({ branch_id: z.string() }))
    .parse(await sqlAll(db, "SELECT DISTINCT branch_id FROM events", []));
  const found = [];
  for (const { branch_id } of branches)
    for (const e of await knownEventsOf(on.store, alice.tenant, branch_id))
      if (e.type === "user_input") found.push(e);
  return found;
}

test("a claimed hops of 8 is REJECTED with call chain too deep, and nothing is stored", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const refused = await task(
    await on.rpc("SendMessage", claiming("m1", { hops: 8 }), { as: alice }),
  );
  expect(refused.status.state).toBe("TASK_STATE_REJECTED");
  expect(refused.status.message?.parts).toEqual([
    { text: "call chain too deep" },
  ]);
  // No run started, so a loop a liar induces costs us nothing at all.
  expect(await inputs(on)).toHaveLength(0);
});

test("a deeper claimed hop count is refused too, and a shallower one runs", async () => {
  const on = await serve({
    agents: { support: talker("hi", "hi") },
    a2a: { expose: ONE },
  });
  expect(
    (
      await task(
        await on.rpc("SendMessage", claiming("deep", { hops: 40 }), {
          as: alice,
        }),
      )
    ).status.state,
  ).toBe("TASK_STATE_REJECTED");
  const shallow = await task(
    await on.rpc("SendMessage", claiming("ok", { hops: 7 }), { as: alice }),
  );
  expect(shallow.status.state).not.toBe("TASK_STATE_REJECTED");
});

test("the rejection is derived, so a retry of the same message answers identically", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const first = await task(
    await on.rpc("SendMessage", claiming("m1", { hops: 9 }), { as: alice }),
  );
  const again = await task(
    await on.rpc("SendMessage", claiming("m1", { hops: 9 }), { as: alice }),
  );
  expect(again.id).toBe(first.id);
  expect(again.status.state).toBe("TASK_STATE_REJECTED");
});

test("a claim is recorded as an untrusted claim and never as the run's principal", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const claim = {
    hops: 2,
    request_id: "partner-1",
    // A caller asserting it is somebody else must change nothing about who it is to us.
    principal: { issuer: "evil", tenant: "acme", subject: "root" },
  };
  const sent = await task(
    await on.rpc("SendMessage", claiming("m1", claim), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const [input] = await inputs(on);
  if (input === undefined) throw new Error("the run recorded no input");
  // The claim is kept, verbatim, as a claim.
  expect(input.data.a2a?.claims).toEqual(claim);
  // And the actor is the authenticated caller, not the one the claim named.
  expect(principalKey(input.actor.principal)).toBe(principalKey(alice));
});

test("a message with no provenance metadata records no claim", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hello"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const [input] = await inputs(on);
  expect(input?.data.a2a?.claims).toBeUndefined();
  // The message and context are still recorded, which is what a retry is answered from.
  expect(input?.data.a2a?.message_id).toBe("m1");
  expect(input?.data.a2a?.context_id).toBe(sent.contextId);
});

test("metadata that is not an object is ignored rather than refused", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", claiming("m1", "not an object"), { as: alice }),
  );
  expect(sent.status.state).not.toBe("TASK_STATE_REJECTED");
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  expect((await inputs(on))[0]?.data.a2a?.claims).toBeUndefined();
});
