import { afterEach, expect, test } from "bun:test";
import { storeConnection } from "@threads/core/host";
import { z } from "zod";
import { alice, bob } from "../kit";
import { sqlAll } from "../sql";
import { talker } from "./agents";
import {
  faultName,
  message,
  reaches,
  result,
  serve,
  stopAll,
  task,
} from "./kit";

// The promise our idempotent-send extension makes: the same messageId from the same authenticated
// caller produces one task and one run. The receipt lookup happens before any contextId is minted,
// so a retry that carries no contextId still answers the original task and the original context.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

const up = () =>
  serve({
    agents: { support: talker("one", "two", "three", "four") },
    a2a: { expose: ONE },
  });

/** Every user_input in the store, across every tenant: how many runs really started. */
async function inputs(
  store: Parameters<typeof sqlAll>[0] extends never
    ? never
    : Awaited<ReturnType<typeof up>>["store"],
): Promise<number> {
  const { db } = await storeConnection(store);
  const rows = z
    .array(z.strictObject({ n: z.number() }))
    .parse(
      await sqlAll(
        db,
        "SELECT COUNT(*) AS n FROM events WHERE type = 'user_input'",
        [],
      ),
    );
  return rows[0]?.n ?? 0;
}

test("the same messageId twice returns one task and starts one run", async () => {
  const on = await up();
  const first = await task(
    await on.rpc("SendMessage", message("dup", "hello"), { as: alice }),
  );
  const second = await task(
    await on.rpc("SendMessage", message("dup", "hello"), { as: alice }),
  );
  expect(second.id).toBe(first.id);
  expect(await inputs(on.store)).toBe(1);
});

test("a retry that changes the contextId is a different message, so it is refused", async () => {
  const on = await up();
  await task(
    await on.rpc("SendMessage", message("ctx", "hello", { contextId: "c-1" }), {
      as: alice,
    }),
  );
  // A resend is byte-identical by contract; a body that differs by a field is another message,
  // and answering the first task for it would hide a client bug rather than report it.
  const moved = await on.rpc(
    "SendMessage",
    message("ctx", "hello", { contextId: "c-2" }),
    { as: alice },
  );
  expect(await faultName(moved)).toBe("InvalidParamsError");
  expect(await inputs(on.store)).toBe(1);
});

test("an identical retry that carries a contextId returns the same task and context", async () => {
  const on = await up();
  const first = await task(
    await on.rpc("SendMessage", message("ctx", "hello", { contextId: "c-1" }), {
      as: alice,
    }),
  );
  expect(first.contextId).toBe("c-1");
  const retry = await task(
    await on.rpc("SendMessage", message("ctx", "hello", { contextId: "c-1" }), {
      as: alice,
    }),
  );
  expect(retry.id).toBe(first.id);
  expect(retry.contextId).toBe("c-1");
  expect(await inputs(on.store)).toBe(1);
});

test("a retry with no contextId returns the same task and the same contextId", async () => {
  const on = await up();
  const first = await task(
    await on.rpc("SendMessage", message("mint", "hello"), { as: alice }),
  );
  expect(first.contextId).toBeString();
  const retry = await task(
    await on.rpc("SendMessage", message("mint", "hello"), { as: alice }),
  );
  // The minted context was recorded in the run's own user_input, so it survives the retry.
  expect(retry.contextId).toBe(first.contextId);
});

test("a reused messageId with another body is InvalidParamsError", async () => {
  const on = await up();
  await task(
    await on.rpc("SendMessage", message("re", "hello"), { as: alice }),
  );
  const other = await on.rpc("SendMessage", message("re", "something else"), {
    as: alice,
  });
  expect(await faultName(other)).toBe("InvalidParamsError");
  expect(await inputs(on.store)).toBe(1);
});

test("the same messageId from two principals gives two tasks", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("shared", "hello"), { as: alice }),
  );
  const theirs = await task(
    await on.rpc("SendMessage", message("shared", "hello"), { as: bob }),
  );
  expect(theirs.id).not.toBe(mine.id);
  expect(theirs.contextId).not.toBe(mine.contextId);
  expect(await inputs(on.store)).toBe(2);
});

test("two principals sharing a contextId do not share a thread", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("a", "hello", { contextId: "same" }), {
      as: alice,
    }),
  );
  const theirs = await task(
    await on.rpc("SendMessage", message("b", "hello", { contextId: "same" }), {
      as: bob,
    }),
  );
  // Same contextId, different derived thread: neither can reach the other by guessing one.
  expect(theirs.id).not.toBe(mine.id);
  const cross = await on.rpc("GetTask", { id: mine.id }, { as: bob });
  expect(await faultName(cross)).toBe("TaskNotFoundError");
});

test("a taskId belonging to another principal is TaskNotFoundError", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("m", "hello"), { as: alice }),
  );
  for (const method of ["GetTask", "CancelTask", "SubscribeToTask"]) {
    const response = await on.rpc(method, { id: mine.id }, { as: bob });
    expect(await faultName(response)).toBe("TaskNotFoundError");
  }
});

test("a task that never existed answers exactly as another principal's does", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("m", "hello"), { as: alice }),
  );
  const theirs = await on.rpc("GetTask", { id: mine.id }, { as: bob });
  const absent = await on.rpc(
    "GetTask",
    { id: "01a00000-0000-7000-8000-000000000000" },
    { as: bob },
  );
  expect(await faultName(theirs)).toBe(await faultName(absent));
});

test("a messageId over 255 bytes is InvalidParamsError", async () => {
  const on = await up();
  const long = "x".repeat(256);
  expect(
    await faultName(
      await on.rpc("SendMessage", message(long, "hello"), { as: alice }),
    ),
  ).toBe("InvalidParamsError");
  // 255 is accepted, so the refusal is the length and not the shape.
  const ok = await on.rpc("SendMessage", message("y".repeat(255), "hello"), {
    as: alice,
  });
  expect((await task(ok)).id).toBeString();
});

const Listed = z.object({
  tasks: z.array(z.looseObject({ id: z.string(), contextId: z.string() })),
  totalSize: z.number(),
});

test("ListTasks shows only the caller's own tasks", async () => {
  const on = await up();
  const mine = await task(
    await on.rpc("SendMessage", message("m1", "hello"), { as: alice }),
  );
  await task(await on.rpc("SendMessage", message("b1", "hello"), { as: bob }));
  const listed = Listed.parse(
    await result(await on.rpc("ListTasks", {}, { as: alice })),
  );
  expect(listed.tasks.map((t) => t.id)).toEqual([mine.id]);
  expect(listed.totalSize).toBe(1);
  const theirs = Listed.parse(
    await result(await on.rpc("ListTasks", {}, { as: bob })),
  );
  expect(theirs.tasks.map((t) => t.id)).not.toContain(mine.id);
});

test("ListTasks filters by contextId", async () => {
  const on = await up();
  const here = await task(
    await on.rpc("SendMessage", message("m1", "hello", { contextId: "here" }), {
      as: alice,
    }),
  );
  const there = await task(
    await on.rpc(
      "SendMessage",
      message("m2", "hello", { contextId: "there" }),
      {
        as: alice,
      },
    ),
  );
  const filtered = Listed.parse(
    await result(
      await on.rpc("ListTasks", { contextId: "here" }, { as: alice }),
    ),
  );
  expect(filtered.tasks.map((t) => t.id)).toEqual([here.id]);
  const both = Listed.parse(
    await result(await on.rpc("ListTasks", {}, { as: alice })),
  );
  expect(both.tasks.map((t) => t.id).toSorted()).toEqual(
    [here.id, there.id].toSorted(),
  );
});

test("ListTasks pages, and a malformed page token is InvalidParamsError", async () => {
  const on = await up();
  for (const id of ["p1", "p2", "p3"])
    await task(
      await on.rpc("SendMessage", message(id, "hello", { contextId: id }), {
        as: alice,
      }),
    );
  const page = Listed.extend({ nextPageToken: z.string() }).parse(
    await result(await on.rpc("ListTasks", { pageSize: 2 }, { as: alice })),
  );
  expect(page.tasks).toHaveLength(2);
  expect(page.totalSize).toBe(3);
  const next = Listed.extend({ nextPageToken: z.string() }).parse(
    await result(
      await on.rpc(
        "ListTasks",
        { pageSize: 2, pageToken: page.nextPageToken },
        { as: alice },
      ),
    ),
  );
  expect(next.tasks).toHaveLength(1);
  expect(next.nextPageToken).toBe("");
  expect(
    await faultName(
      await on.rpc("ListTasks", { pageToken: "not-a-token" }, { as: alice }),
    ),
  ).toBe("InvalidParamsError");
});

test("ListTasks shows a finished task with the state it ended in", async () => {
  const on = await up();
  const sent = await task(
    await on.rpc("SendMessage", message("done", "hello"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const listed = Listed.parse(
    await result(await on.rpc("ListTasks", {}, { as: alice })),
  );
  expect(listed.tasks).toHaveLength(1);
  expect(listed.tasks[0]).toMatchObject({
    id: sent.id,
    status: { state: "TASK_STATE_COMPLETED" },
  });
});
