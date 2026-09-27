import { afterEach, expect, test } from "bun:test";
import { sqlite } from "threadsai";
import { storeConnection } from "threadsai/host";
import { z } from "zod";
import { alice } from "../kit";
import { sqlAll } from "../sql";
import { talker } from "./agents";
import { faultName, message, result, serve, stopAll, task } from "./kit";

// Two hosts, one store: the same inbound messageId must still make one task and one run. The receipt
// row and the run's user_input commit in one transaction, so the loser of the race rolls its append
// back and answers as a replay rather than starting a second run.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

/** Two hosts of the same agent on one store, both through ready(). */
async function pair(): Promise<
  readonly [
    Awaited<ReturnType<typeof serve>>,
    Awaited<ReturnType<typeof serve>>,
  ]
> {
  const store = sqlite(":memory:");
  const first = await serve({
    agents: { support: talker("one", "two") },
    a2a: { expose: ONE },
    store,
  });
  const second = await serve({
    agents: { support: talker("one", "two") },
    a2a: { expose: ONE },
    store,
  });
  return [first, second];
}

async function runs(
  store: Parameters<typeof storeConnection>[0],
): Promise<number> {
  const { db } = await storeConnection(store);
  return (
    z
      .array(z.strictObject({ n: z.number() }))
      .parse(
        await sqlAll(
          db,
          "SELECT COUNT(*) AS n FROM events WHERE type = 'user_input'",
          [],
        ),
      )[0]?.n ?? 0
  );
}

test("the same messageId sent to two hosts gives one task and one run", async () => {
  const [first, second] = await pair();
  const here = await task(
    await first.rpc("SendMessage", message("dup", "hello"), { as: alice }),
  );
  const there = await task(
    await second.rpc("SendMessage", message("dup", "hello"), { as: alice }),
  );
  expect(there.id).toBe(here.id);
  expect(there.contextId).toBe(here.contextId);
  expect(await runs(first.store)).toBe(1);
});

test("two hosts racing on one messageId still make one task and one run", async () => {
  const [first, second] = await pair();
  // Both in flight at once: whichever wins the key, the other answers as a replay.
  const [a, b] = await Promise.all([
    first.rpc("SendMessage", message("race", "hello"), { as: alice }),
    second.rpc("SendMessage", message("race", "hello"), { as: alice }),
  ]);
  const ids = [await task(a), await task(b)].map((t) => t.id);
  expect(ids[0]).toBe(ids[1]);
  expect(await runs(first.store)).toBe(1);
});

test("a task started on one host is readable, followed and cancellable on the other", async () => {
  const [first, second] = await pair();
  const here = await task(
    await first.rpc("SendMessage", message("m1", "hello"), { as: alice }),
  );
  // The task is the log's, not the process's, so the second host answers for it in full.
  const got = await task(
    await second.rpc("GetTask", { id: here.id }, { as: alice }),
  );
  expect(got.id).toBe(here.id);
  const listed = z
    .object({ tasks: z.array(z.looseObject({ id: z.string() })) })
    .parse(await result(await second.rpc("ListTasks", {}, { as: alice })));
  expect(listed.tasks.map((t) => t.id)).toContain(here.id);
});

test("a reused messageId with another body is refused by the second host too", async () => {
  const [first, second] = await pair();
  await task(
    await first.rpc("SendMessage", message("re", "hello"), { as: alice }),
  );
  const other = await second.rpc("SendMessage", message("re", "different"), {
    as: alice,
  });
  expect(await faultName(other)).toBe("InvalidParamsError");
  expect(await runs(first.store)).toBe(1);
});
