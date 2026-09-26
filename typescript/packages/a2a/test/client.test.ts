import { describe, expect, test } from "bun:test";
import type { Sent, WebTransport } from "@threads/core/adapter";
import { call, fetchBytes } from "../src/protocol/client";
import type { Wire } from "../src/protocol/wire";

// The invariant-3 boundary: one request answers exactly one of five outcomes, and `not_sent` is the
// only one that lets a caller send again on its own. Each test here drives one decision, so
// widening `not_sent` by a single error code breaks a named test rather than passing quietly.

const WIRE: Wire = {
  url: "https://partner.example/a2a/refunds",
  binding: "JSONRPC",
};
const RPC: Wire = WIRE;
const REST: Wire = {
  url: "https://partner.example/a2a/refunds",
  binding: "HTTP+JSON",
};

const TASK = {
  id: "task-1",
  contextId: "ctx-1",
  status: { state: "TASK_STATE_WORKING" },
};

const sending = (transport: WebTransport) => ({
  transport,
  signal: new AbortController().signal,
  timeoutMs: 5_000,
});

/** A transport that answers one response, and records what it was asked to send. */
function answering(
  make: (url: string, init: Sent) => Response | Promise<Response>,
): WebTransport & { readonly sent: { url: string; init: Sent }[] } {
  const sent: { url: string; init: Sent }[] = [];
  return {
    sent,
    resolve: async () => ["93.184.216.34"],
    fetch: async (url, _address, init) => {
      sent.push({ url, init });
      return await make(url, init);
    },
  };
}

/** A transport that fails the request with a node-style error code. */
function failing(code: string | undefined, name = "Error"): WebTransport {
  return {
    resolve: async () => ["93.184.216.34"],
    fetch: async () => {
      const error = new Error(`transport said ${code ?? name}`);
      error.name = name;
      if (code !== undefined) Object.assign(error, { code });
      throw error;
    },
  };
}

const json = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });

describe("an answer the peer gave", () => {
  test("a JSON-RPC result is ok, unwrapped", async () => {
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: 1, result: { task: TASK } }),
    );
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
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: 1, error: { code: -32001, message: "gone" } }),
    );
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
    const transport = answering(() =>
      json({ code: -32004, message: "terminal" }, 400),
    );
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
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: 1, error: { code: -31999, message: "odd" } }),
    );
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

describe("the request provably never left", () => {
  // Only failures that happen strictly before the request bytes are written. Each of these is a
  // real node error code; if one is ever dropped from the list, this test fails.
  for (const code of [
    "ECONNREFUSED",
    "ENOTFOUND",
    "EAI_AGAIN",
    "EHOSTUNREACH",
    "ENETUNREACH",
    "EADDRNOTAVAIL",
    "ERR_TLS_CERT_ALTNAME_INVALID",
    "CERT_HAS_EXPIRED",
    "DEPTH_ZERO_SELF_SIGNED_CERT",
    "SELF_SIGNED_CERT_IN_CHAIN",
    "UNABLE_TO_VERIFY_LEAF_SIGNATURE",
    "ERR_SSL_WRONG_VERSION_NUMBER",
  ])
    test(`${code} is not_sent`, async () => {
      const answer = await call(
        RPC,
        "SendMessage",
        { message: {} },
        sending(failing(code)),
      );
      expect(answer.kind).toBe("not_sent");
    });

  test("a non-https URL is not_sent, and nothing is dialled", async () => {
    const transport = answering(() => json(TASK));
    const answer = await call(
      { url: "http://partner.example/a2a", binding: "JSONRPC" },
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("not_sent");
    expect(transport.sent).toHaveLength(0);
  });

  test("an address the SSRF guard refuses is not_sent, and nothing is dialled", async () => {
    const sent: { url: string }[] = [];
    const transport: WebTransport = {
      resolve: async () => ["127.0.0.1"],
      fetch: async (url) => {
        sent.push({ url });
        return json(TASK);
      },
    };
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("not_sent");
    expect(sent).toHaveLength(0);
  });
});

describe("the outcome is uncertain", () => {
  // Anything that may have written bytes first. Treating one of these as not_sent would let an
  // effect repeat silently, so each one is pinned.
  for (const code of ["ECONNRESET", "EPIPE", "ETIMEDOUT", "EPROTO", undefined])
    test(`${code ?? "an error with no code"} is unknown, never not_sent`, async () => {
      const answer = await call(
        RPC,
        "SendMessage",
        { message: {} },
        sending(failing(code)),
      );
      expect(answer.kind).toBe("unknown");
    });

  test("a timeout is unknown{timeout}", async () => {
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(failing(undefined, "TimeoutError")),
    );
    expect(answer.kind).toBe("unknown");
    if (answer.kind !== "unknown") throw new Error("unreachable");
    expect(answer.reason).toBe("timeout");
  });

  test("a body that breaks mid-read is unknown, not a fault", async () => {
    const transport = answering(
      () =>
        new Response(
          new ReadableStream<Uint8Array>({
            start(controller) {
              controller.enqueue(new TextEncoder().encode('{"task":'));
              controller.error(new Error("connection dropped"));
            },
          }),
          { headers: { "content-type": "application/json" } },
        ),
    );
    const answer = await call(
      RPC,
      "SendMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("unknown");
  });

  test("an unparsable answer is a fault: the peer replied, so nothing is in doubt", async () => {
    const transport = answering(
      () =>
        new Response("not json", {
          headers: { "content-type": "application/json" },
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
});

describe("a streaming answer", () => {
  test("SSE items parse as StreamResponse, in order", async () => {
    const body = [
      `data: {"jsonrpc":"2.0","id":1,"result":{"task":${JSON.stringify(TASK)}}}\n\n`,
      'data: {"jsonrpc":"2.0","id":1,"result":{"statusUpdate":{"taskId":"task-1","contextId":"ctx-1","status":{"state":"TASK_STATE_COMPLETED"}}}}\n\n',
    ].join("");
    const transport = answering(
      () =>
        new Response(body, {
          headers: { "content-type": "text/event-stream" },
        }),
    );
    const answer = await call(
      RPC,
      "SendStreamingMessage",
      { message: {} },
      sending(transport),
    );
    expect(answer.kind).toBe("stream");
    if (answer.kind !== "stream") throw new Error("unreachable");
    const items = await Array.fromAsync(answer.items);
    expect(items).toHaveLength(2);
    expect(items[0]?.task?.id).toBe("task-1");
    expect(items[1]?.statusUpdate?.status.state).toBe("TASK_STATE_COMPLETED");
  });

  test("an item that is not a StreamResponse ends the stream rather than being guessed at", async () => {
    const body = `data: {"task":${JSON.stringify(TASK)}}\n\ndata: {"unknownField":1}\n\ndata: {"message":{"messageId":"m","role":"ROLE_AGENT","parts":[]}}\n\n`;
    const transport = answering(
      () =>
        new Response(body, {
          headers: { "content-type": "text/event-stream" },
        }),
    );
    const answer = await call(
      REST,
      "SendStreamingMessage",
      { message: {} },
      sending(transport),
    );
    if (answer.kind !== "stream") throw new Error("unreachable");
    expect(await Array.fromAsync(answer.items)).toHaveLength(1);
  });
});

describe("what goes on the wire", () => {
  test("every request declares A2A-Version 1.0 and asks for a2a+json", async () => {
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: 1, result: { task: TASK } }),
    );
    await call(RPC, "SendMessage", { message: {} }, sending(transport));
    const sent = transport.sent[0]?.init;
    if (sent === undefined) throw new Error("the transport was asked to send");
    expect(sent.headers["A2A-Version"]).toBe("1.0");
    expect(sent.headers["content-type"]).toBe("application/a2a+json");
  });

  test("the credential rides in the header only, and extensions are declared when relied on", async () => {
    const transport = answering(() =>
      json({ jsonrpc: "2.0", id: 1, result: { task: TASK } }),
    );
    await call(
      RPC,
      "SendMessage",
      { message: {} },
      {
        ...sending(transport),
        authorization: "Bearer shhh",
        extensions: ["https://threadsai.dev/a2a/ext/idempotent-send/v1"],
      },
    );
    const sent = transport.sent[0]?.init;
    if (sent === undefined) throw new Error("the transport was asked to send");
    expect(sent.headers["authorization"]).toBe("Bearer shhh");
    expect(sent.headers["A2A-Extensions"]).toBe(
      "https://threadsai.dev/a2a/ext/idempotent-send/v1",
    );
    expect(sent.body ?? "").not.toContain("shhh");
  });

  test("a subscribe is a GET, as the normative proto annotates it", async () => {
    const transport = answering(
      () =>
        new Response("", { headers: { "content-type": "text/event-stream" } }),
    );
    await call(REST, "SubscribeToTask", { id: "task-1" }, sending(transport));
    expect(transport.sent[0]?.init.method).toBe("GET");
    expect(transport.sent[0]?.url).toContain("/tasks/task-1:subscribe");
  });
});

describe("fetching a card", () => {
  test("no credential is sent: discovery is unauthenticated", async () => {
    const transport = answering(() => json({ name: "refunds" }));
    await fetchBytes(
      "https://partner.example/.well-known/agent-card.json",
      4096,
      {
        ...sending(transport),
        authorization: "Bearer shhh",
      },
    );
    const sent = transport.sent[0]?.init;
    if (sent === undefined) throw new Error("the transport was asked to send");
    expect(sent.headers["authorization"]).toBeUndefined();
  });

  test("a body over the cap fails rather than being truncated into a card", async () => {
    const transport = answering(() => json({ name: "x".repeat(200) }));
    const got = await fetchBytes(
      "https://partner.example/card.json",
      32,
      sending(transport),
    );
    expect(got.kind).toBe("failed");
    if (got.kind !== "failed") throw new Error("unreachable");
    expect(got.why).toContain("32 bytes");
  });

  test("a non-200 is a failure that names the status", async () => {
    const transport = answering(() => json({}, 503));
    const got = await fetchBytes(
      "https://partner.example/card.json",
      4096,
      sending(transport),
    );
    expect(got.kind).toBe("failed");
    if (got.kind !== "failed") throw new Error("unreachable");
    expect(got.why).toContain("503");
  });
});
