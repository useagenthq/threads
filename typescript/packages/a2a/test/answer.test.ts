import { describe, expect, test } from "bun:test";
import { call } from "../src/protocol/client";
import {
  answering,
  httpErr,
  json,
  REST,
  RPC,
  rpcErr,
  rpcOk,
  sending,
  sentId,
  sse,
  TASK,
} from "./kit";

// Reading a peer's answer (src/protocol/answer.ts). A response is a partner's bytes, so every one of
// these asks the same question: what does this answer have to prove before we read anything out of
// it? It has to be ours, it has to be decodable, and when it is an error it has to name one.

describe("an answer the peer gave", () => {
  test("a JSON-RPC result is ok, unwrapped", async () => {
    const transport = rpcOk({ task: TASK });
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("ok");
    if (answer.kind !== "ok") throw new Error("unreachable");
    expect(answer.value).toEqual({ task: TASK });
  });

  test("an HTTP+JSON body is ok as it came", async () => {
    const transport = answering(() => json(TASK));
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("ok");
    // A GET carries its fields in the query string, and the id in the path.
    expect(transport.sent[0]?.url).toContain("/tasks/task-1");
    expect(transport.sent[0]?.init.body).toBeUndefined();
  });

  test("a JSON-RPC error is a fault, named by its code, not a doubt", async () => {
    const transport = rpcErr({ code: -32001, message: "gone" });
    const answer = await call(
      RPC,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("TaskNotFoundError");
  });

  test("an HTTP+JSON error status carries its A2A code", async () => {
    const transport = httpErr("UnsupportedOperationError", 400);
    const answer = await call(
      REST,
      "SubscribeToTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("UnsupportedOperationError");
  });

  test("a code outside the table is an InternalError that quotes the peer", async () => {
    const transport = rpcErr({ code: -31999, message: "odd" });
    const answer = await call(
      RPC,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InternalError");
    expect(answer.fault.message).toContain("-31999");
  });
});

/** The items of a stream answer, or a thrown error if the answer was not a stream at all. */
async function streamOf(answer: Awaited<ReturnType<typeof call>>) {
  if (answer.kind !== "stream")
    throw new Error(`expected a stream, got ${answer.kind}`);
  return await Array.fromAsync(answer.items);
}

const STATUS_UPDATE =
  '{"statusUpdate":{"taskId":"task-1","contextId":"ctx-1","status":{"state":"TASK_STATE_COMPLETED"}}}';

describe("a streaming answer", () => {
  test("SSE items parse as StreamResponse, in order", async () => {
    const transport = sse((id) => [
      `{"jsonrpc":"2.0","id":${JSON.stringify(id)},"result":{"task":${JSON.stringify(TASK)}}}`,
      `{"jsonrpc":"2.0","id":${JSON.stringify(id)},"result":${STATUS_UPDATE}}`,
    ]);
    const items = await streamOf(
      await call(
        RPC,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    expect(items.map((i) => i.kind)).toEqual(["item", "item"]);
    if (items[0]?.kind !== "item" || items[1]?.kind !== "item")
      throw new Error("unreachable");
    expect(items[0].item.task?.id).toBe("task-1");
    expect(items[1].item.statusUpdate?.status.state).toBe(
      "TASK_STATE_COMPLETED",
    );
  });

  test("an item that is not a StreamResponse refuses, so a caller sees why the stream ended", async () => {
    const transport = sse(() => [
      `{"task":${JSON.stringify(TASK)}}`,
      '{"unknownField":1}',
      '{"message":{"messageId":"m","role":"ROLE_AGENT","parts":[{"text":"hi"}]}}',
    ]);
    const items = await streamOf(
      await call(
        REST,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    // The good frame, then the refusal, and nothing after it.
    expect(items.map((i) => i.kind)).toEqual(["item", "refused"]);
    if (items[1]?.kind !== "refused") throw new Error("unreachable");
    expect(items[1].fault.name).toBe("InvalidAgentResponseError");
  });

  test("a frame that is not JSON refuses rather than ending the stream silently", async () => {
    const transport = sse(() => ["not json at all"]);
    const items = await streamOf(
      await call(
        REST,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    expect(items.map((i) => i.kind)).toEqual(["refused"]);
  });

  test("a stream that is not UTF-8 refuses instead of yielding replacement characters", async () => {
    const transport = answering(
      () =>
        new Response(
          Uint8Array.of(
            ...new TextEncoder().encode('data: {"text":"'),
            0xff,
            ...new TextEncoder().encode('"}\n\n'),
          ),
          { headers: { "content-type": "text/event-stream" } },
        ),
    );
    const items = await streamOf(
      await call(
        REST,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    expect(items.map((i) => i.kind)).toEqual(["refused"]);
    if (items[0]?.kind !== "refused") throw new Error("unreachable");
    expect(items[0].fault.message).toContain("UTF-8");
  });

  test("an unterminated line past the frame budget refuses rather than being buffered and dropped", async () => {
    const transport = answering(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              const chunk = new TextEncoder().encode("a".repeat(256 * 1024));
              // 2 MiB with no line break at all.
              for (let i = 0; i < 8; i += 1) controller.enqueue(chunk);
              controller.close();
            },
          }),
          { headers: { "content-type": "text/event-stream" } },
        ),
    );
    const items = await streamOf(
      await call(
        REST,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    expect(items.map((i) => i.kind)).toEqual(["refused"]);
    if (items[0]?.kind !== "refused") throw new Error("unreachable");
    expect(items[0].fault.message).toContain("bytes");
  });
});

describe("an answer has to prove it answers us", () => {
  test("a JSON-RPC result carrying someone else's id is refused, not read", async () => {
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: "not-our-id", result: { task: TASK } }),
    );
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
    expect(answer.fault.message).toContain("not-our-id");
  });

  test("the id we send is fresh per request, so one answer cannot be replayed for another", async () => {
    const transport = rpcOk({ task: TASK });
    await call(RPC, "SendMessage", { message: {} }, sending(transport));
    await call(RPC, "SendMessage", { message: {} }, sending(transport));
    const ids = transport.sent.map((s) => sentId(s.init));
    expect(ids[0]).toEqual(expect.any(String));
    expect(ids[0]).not.toEqual(ids[1]);
  });

  test("an SSE frame carrying someone else's id is refused, per frame", async () => {
    const transport = sse((id) => [
      `{"jsonrpc":"2.0","id":${JSON.stringify(id)},"result":{"task":${JSON.stringify(TASK)}}}`,
      `{"jsonrpc":"2.0","id":"not-our-id","result":${STATUS_UPDATE}}`,
    ]);
    const items = await streamOf(
      await call(
        RPC,
        "SendStreamingMessage",
        { message: {} },
        sending(transport),
      ),
    );
    expect(items.map((i) => i.kind)).toEqual(["item", "refused"]);
    if (items[1]?.kind !== "refused") throw new Error("unreachable");
    expect(items[1].fault.message).toContain("not-our-id");
  });

  test("a null id on an error is ours: JSON-RPC requires it when the request id was unreadable", async () => {
    const transport = answering(() =>
      json({
        jsonrpc: "2.0",
        id: null,
        error: { code: -32700, message: "could not read your id" },
      }),
    );
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("JSONParseError");
  });

  test("a null id on a result is not ours: a result cannot claim the id was unreadable", async () => {
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: null, result: { task: TASK } }),
    );
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.name).toBe("InvalidAgentResponseError");
  });

  test("an HTTP+JSON answer has no envelope, so there is nothing to correlate and it is read", async () => {
    const transport = answering(() => json(TASK));
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("ok");
  });
});

describe("a body that is not UTF-8", () => {
  test("is a fault, not replacement characters read on as if the peer sent them", async () => {
    const transport = answering(
      () =>
        new Response(
          Uint8Array.of(
            ...new TextEncoder().encode('{"id":"'),
            0xff,
            ...new TextEncoder().encode('"}'),
          ),
          { headers: { "content-type": "application/json" } },
        ),
    );
    const answer = await call(
      REST,
      "GetTask",
      { id: "task-1" },
      sending(transport),
    );
    expect(answer.kind).toBe("fault");
    if (answer.kind !== "fault") throw new Error("unreachable");
    expect(answer.fault.message).toContain("UTF-8");
  });
});
