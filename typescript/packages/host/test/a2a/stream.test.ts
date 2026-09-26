import { afterEach, expect, test } from "bun:test";
import { z } from "zod";
import { alice } from "../kit";
import { asker, talker } from "./agents";
import {
  faultName,
  frames,
  message,
  reaches,
  serve,
  stopAll,
  task,
} from "./kit";

// The streams. Every frame is a function of the committed run slice, so a resume at any frame
// boundary yields exactly the frames after it: the assertion below replays from every single
// boundary and demands the remainder, with no frame delivered twice and none skipped.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

const Item = z.looseObject({
  task: z.unknown().optional(),
  statusUpdate: z
    .looseObject({ status: z.looseObject({ state: z.string() }) })
    .optional(),
  artifactUpdate: z.looseObject({ artifact: z.looseObject({}) }).optional(),
});

const Envelope = z.looseObject({ result: z.unknown() });

/** A frame's StreamResponse, out of the JSON-RPC envelope when it is in one. */
function itemOf(data: unknown): z.infer<typeof Item> {
  const wrapped = Envelope.safeParse(data);
  return Item.parse(
    wrapped.success && wrapped.data.result !== undefined
      ? wrapped.data.result
      : data,
  );
}

/** Each frame's kind and, for a status update, its state: the stream's shape without its ids. */
function shape(read: Awaited<ReturnType<typeof frames>>): readonly string[] {
  return read.map((f) => {
    const item = itemOf(f.data);
    if (item.task !== undefined) return "task";
    if (item.statusUpdate !== undefined)
      return `status:${item.statusUpdate.status.state}`;
    if (item.artifactUpdate !== undefined) return "artifact";
    return "other";
  });
}

test("a streaming send's frames are a task snapshot, then status changes, then the artifact", async () => {
  const on = await serve({
    agents: { support: talker("the answer") },
    a2a: { expose: ONE },
  });
  const response = await on.rpc("SendStreamingMessage", message("m1", "hi"), {
    as: alice,
  });
  expect(response.headers.get("content-type")).toBe("text/event-stream");
  const read = await frames(response);
  expect(shape(read)).toEqual([
    "task",
    "status:TASK_STATE_WORKING",
    "status:TASK_STATE_COMPLETED",
    "artifact",
  ]);
  // Every frame carries a resumable position, and they only ever move forwards.
  const ids = read.map((f) => f.id ?? "");
  expect(ids.every((id) => /^\d+:\d+$/.test(id))).toBe(true);
  expect(ids).toEqual([...new Set(ids)]);
});

/** A task parked on a question: interrupted, not terminal, so it can still be subscribed to. */
const parked = () =>
  serve({
    agents: {
      support: asker({
        question: "Which colour?",
        options: ["red", "blue"],
        answer: "done",
      }),
    },
    a2a: { expose: ONE },
  });

async function waiting(on: Awaited<ReturnType<typeof serve>>): Promise<string> {
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
  return sent.id;
}

/** The stream from the very beginning: every frame of the task as it stands. */
async function whole(
  on: Awaited<ReturnType<typeof serve>>,
  taskId: string,
  from = "0:0",
): Promise<Awaited<ReturnType<typeof frames>>> {
  return frames(
    await on.http("GET", `/tasks/${taskId}:subscribe`, {
      as: alice,
      headers: { "last-event-id": from },
    }),
  );
}

test("a resume at every frame boundary yields exactly the remaining frames, with no duplicate", async () => {
  const on = await parked();
  const taskId = await waiting(on);
  const all = await whole(on, taskId);
  expect(all.length).toBeGreaterThan(2);
  for (const [at, frame] of all.entries()) {
    const rest = await whole(on, taskId, frame.id ?? "");
    // Exactly what follows this frame: nothing repeated, nothing lost.
    expect(rest.map((f) => f.id)).toEqual(all.slice(at + 1).map((f) => f.id));
    expect(rest.map((f) => JSON.stringify(f.data))).toEqual(
      all.slice(at + 1).map((f) => JSON.stringify(f.data)),
    );
  }
});

test("the same committed run streams byte-identical frames every time", async () => {
  const on = await parked();
  const taskId = await waiting(on);
  expect(JSON.stringify(await whole(on, taskId))).toBe(
    JSON.stringify(await whole(on, taskId)),
  );
});

test("a resume past every frame yields nothing and closes", async () => {
  const on = await parked();
  const taskId = await waiting(on);
  const all = await whole(on, taskId);
  const last = all.at(-1)?.id ?? "";
  expect(await whole(on, taskId, last)).toEqual([]);
});

test("a malformed Last-Event-ID is InvalidParamsError", async () => {
  const on = await serve({
    agents: { support: asker({ question: "Which?", answer: "done" }) },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  const response = await on.http("GET", `/tasks/${sent.id}:subscribe`, {
    as: alice,
    headers: { "last-event-id": "not-a-cursor" },
  });
  expect(await faultName(response)).toBe("InvalidParamsError");
});

test("SubscribeToTask on a terminal task is UnsupportedOperationError, and GetTask then has the result", async () => {
  const on = await serve({
    agents: { support: talker("the answer") },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  const late = await on.rpc("SubscribeToTask", { id: sent.id }, { as: alice });
  expect(await faultName(late)).toBe("UnsupportedOperationError");
  // The required fallback: a reconnecting follower reads the finished task instead.
  const got = await task(
    await on.rpc("GetTask", { id: sent.id }, { as: alice }),
  );
  expect(got.status.state).toBe("TASK_STATE_COMPLETED");
  expect(got.artifacts?.[0]?.parts).toEqual([{ text: "the answer" }]);
});

test("the subscribe path accepts both GET and POST", async () => {
  const on = await serve({
    agents: { support: asker({ question: "Which?", answer: "done" }) },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc("SendMessage", message("m1", "hi"), { as: alice }),
  );
  await reaches(on, alice, sent.id, ["TASK_STATE_INPUT_REQUIRED"]);
  for (const verb of ["GET", "POST"]) {
    const response = await on.http(verb, `/tasks/${sent.id}:subscribe`, {
      as: alice,
    });
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    await response.body?.cancel();
  }
});

test("the JSON-RPC binding wraps each stream item in its envelope and HTTP+JSON does not", async () => {
  const on = await serve({
    agents: { support: talker("the answer", "the answer") },
    a2a: { expose: ONE },
  });
  const rpc = await frames(
    await on.raw("POST", "/a2a/support", {
      as: alice,
      body: {
        jsonrpc: "2.0",
        id: "s-1",
        method: "SendStreamingMessage",
        params: message("m1", "hi"),
      },
    }),
  );
  const Wrapped = z.strictObject({
    jsonrpc: z.literal("2.0"),
    id: z.literal("s-1"),
    result: Item,
  });
  for (const frame of rpc) Wrapped.parse(frame.data);
  const http = await frames(
    await on.http("POST", "/message:stream", {
      as: alice,
      body: message("m2", "hi"),
    }),
  );
  // Bare StreamResponse items: no envelope, no id, no jsonrpc.
  for (const frame of http) {
    expect(Wrapped.safeParse(frame.data).success).toBe(false);
    Item.parse(frame.data);
  }
  expect(shape(http)).toEqual(shape(rpc));
});

test("a stream closes at an interrupted state, so a question does not hold it open", async () => {
  const on = await serve({
    agents: {
      support: asker({
        question: "Which colour?",
        options: ["red", "blue"],
        answer: "done",
      }),
    },
    a2a: { expose: ONE },
  });
  const read = await frames(
    await on.rpc("SendStreamingMessage", message("m1", "hi"), { as: alice }),
  );
  expect(shape(read).at(-1)).toBe("status:TASK_STATE_INPUT_REQUIRED");
  // The question and its options travelled in the frame, so the client can answer from the stream.
  const last = itemOf(read.at(-1)?.data);
  expect(JSON.stringify(last.statusUpdate)).toInclude("Which colour?");
});

test("a streaming send that is refused answers the refusal, not a stream", async () => {
  const on = await serve({
    agents: { support: talker("hi") },
    a2a: { expose: ONE },
  });
  const response = await on.rpc(
    "SendStreamingMessage",
    {
      message: {
        messageId: "f1",
        role: "ROLE_USER",
        parts: [{ raw: "AAA" }],
      },
    },
    { as: alice },
  );
  expect(await faultName(response)).toBe("ContentTypeNotSupportedError");
});
