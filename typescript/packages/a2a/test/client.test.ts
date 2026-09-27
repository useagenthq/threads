import { describe, expect, test } from "bun:test";
import type { WebTransport } from "threadsai/adapter";
import { call, fetchBytes } from "../src/protocol/client";
import {
  answering,
  failing,
  json,
  REST,
  RPC,
  rpcOk,
  sending,
  TASK,
} from "./kit";

// The invariant-3 boundary: one request answers exactly one of five outcomes, and `not_sent` is the
// only one that lets a caller send again on its own. Each test here drives one decision, so widening
// `not_sent` by a single error code breaks a named test rather than passing quietly. What a peer's
// answer has to prove before it is read is in answer.test.ts.

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

  test("a URL with credentials is not_sent, which is why one must never be pinned", async () => {
    // The guard does catch this, but only at call time. Pinning such an interface is therefore an
    // availability bug, not a way past the guard: every call on that remote answers not_sent
    // forever. pin.test.ts is what keeps one from being pinned in the first place.
    const transport = answering(() => json(TASK));
    const answer = await call(
      { url: "https://user:pass@partner.example/a2a", binding: "JSONRPC" },
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

describe("what goes on the wire", () => {
  test("every request declares A2A-Version 1.0 and asks for a2a+json", async () => {
    const transport = rpcOk({ task: TASK });
    await call(RPC, "SendMessage", { message: {} }, sending(transport));
    const sent = transport.sent[0]?.init;
    if (sent === undefined) throw new Error("the transport was asked to send");
    expect(sent.headers["A2A-Version"]).toBe("1.0");
    expect(sent.headers["content-type"]).toBe("application/a2a+json");
  });

  test("the credential rides in the header only, and extensions are declared when relied on", async () => {
    const transport = rpcOk({ task: TASK });
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
