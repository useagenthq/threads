import { describe, expect, test } from "bun:test";
import { outbound, sentRpcId } from "../src/protocol/wire";
import { REST, RPC } from "./kit";

// The bytes one call becomes. These are byte-exact on purpose: the same literals are asserted in
// python/tests/a2a/test_wire.py, which is what makes cross-language byte identity a contract rather
// than a coincidence. Python's default json.dumps separators put a space after every comma and
// colon, so the two languages used to write different bytes for the same call.

const MESSAGE = {
  messageId: "m1",
  role: "ROLE_USER",
  parts: [{ text: "hi" }],
};

describe("the envelope bytes", () => {
  test("a JSON-RPC body is written the way json.dumps must also write it", () => {
    expect(
      outbound(RPC, "SendMessage", { message: MESSAGE }, "req-1").body,
    ).toBe(
      '{"jsonrpc":"2.0","id":"req-1","method":"SendMessage","params":' +
        '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}}',
    );
  });

  test("the id we sent comes back out for the caller to correlate", () => {
    expect(
      outbound(RPC, "SendMessage", { message: MESSAGE }, "req-1").rpcId,
    ).toBe("req-1");
  });

  test("an HTTP+JSON body is the request message alone", () => {
    expect(
      outbound(REST, "SendMessage", { message: MESSAGE }, "req-1").body,
    ).toBe(
      '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"hi"}]}}',
    );
  });

  test("an HTTP+JSON request carries no envelope, so there is no id", () => {
    // Nothing to correlate: the binding has no envelope to put an id in.
    expect(
      outbound(REST, "SendMessage", { message: MESSAGE }, "req-1").rpcId,
    ).toBeUndefined();
  });

  test("a GET puts its non-string fields in the query as compact JSON", () => {
    const built = outbound(REST, "ListTasks", { pageSize: 10 }, "req-1");
    expect(built.body).toBeUndefined();
    expect(built.url).toContain("pageSize=10");
  });
});

describe("the id an answer has to carry back", () => {
  const built = (rpcId: string) =>
    outbound(RPC, "SendMessage", { message: MESSAGE }, rpcId);

  test("is this request's own id when nothing replaces the body", () => {
    expect(sentRpcId(built("req-1"), undefined)).toBe("req-1");
  });

  test("is the stored body's id when a re-dispatch replays it", () => {
    // A re-dispatch sends the bytes its first attempt stored, so correlating against an id generated
    // for this attempt would refuse the peer's answer to the request we actually sent.
    const stored = outbound(
      RPC,
      "SendMessage",
      { message: MESSAGE },
      "stored-1",
    ).body;
    if (stored === undefined) throw new Error("SendMessage has a body");
    expect(sentRpcId(built("fresh-2"), stored)).toBe("stored-1");
  });

  test("is nothing on HTTP+JSON, which has no envelope to put an id in", () => {
    const rest = outbound(REST, "SendMessage", { message: MESSAGE }, "req-1");
    expect(sentRpcId(rest, undefined)).toBeUndefined();
    expect(sentRpcId(rest, '{"id":"not-an-envelope-id"}')).toBeUndefined();
  });

  test("is nothing when a replayed body carries no string id", () => {
    expect(sentRpcId(built("req-1"), "not json")).toBeUndefined();
    expect(sentRpcId(built("req-1"), '{"id":7}')).toBeUndefined();
  });
});
