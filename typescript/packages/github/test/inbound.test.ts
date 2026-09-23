import { describe, expect, test } from "bun:test";
import { Inbound, secret } from "@threads/core/adapter";
import { github } from "../src";
import { adapter, alice, CHALLENGE, commentPayload, request } from "./kit";

const principal = { issuer: "github:99", tenant: "github:99", subject: "42" };

describe("verify", () => {
  test("a good signature yields tenant, installation and delivery", () => {
    expect(adapter.verify(request(commentPayload("hi")))).toEqual({
      ok: true,
      value: { tenant: "github:99", installation_id: "99", delivery_id: "d-1" },
    });
  });

  test.each([
    ["bad signature", { "x-hub-signature-256": `sha256=${"0".repeat(64)}` }],
    ["short signature", { "x-hub-signature-256": "sha256=abcd" }],
    ["missing signature", { "x-hub-signature-256": undefined }],
    ["missing delivery id", { "x-github-delivery": undefined }],
  ])("%s is unverified", (_, headers) => {
    const result = adapter.verify(
      request(commentPayload("hi"), "issue_comment", headers),
    );
    expect(result.ok ? "ok" : result.error.code).toBe("unverified");
  });

  test("a tampered body is unverified", () => {
    const raw = request(commentPayload("hi"));
    const body = new TextEncoder().encode(
      JSON.stringify(commentPayload("bye")),
    );
    const result = adapter.verify({ ...raw, body });
    expect(result.ok).toBe(false);
  });

  test("a payload without an installation is unverified", () => {
    const { installation: _, ...rest } = commentPayload("hi");
    expect(adapter.verify(request(rest)).ok).toBe(false);
  });

  test("an installation the tenant map doesn't know is unverified", () => {
    const mapped = github({
      agent: "triage",
      webhookSecret: secret("THREADS_GH_TEST_WEBHOOK"),
      token: secret("THREADS_GH_TEST_TOKEN"),
      tenant: (id) => (id === "1" ? "acme" : undefined),
    });
    expect(mapped.verify(request(commentPayload("hi"))).ok).toBe(false);
  });
});

describe("parse", () => {
  const parsed = (raw: ReturnType<typeof request>) => {
    const result = adapter.parse(raw);
    if (!result.ok) throw new Error(result.error.message);
    return result.value.map((item) => Inbound.parse(item));
  };

  test("a new comment is a message keyed by the delivery", () => {
    expect(parsed(request(commentPayload("please look")))).toEqual([
      {
        kind: "message",
        principal,
        address: "acme/app#7",
        item_key: "d-1#0",
        content: "please look",
      },
    ]);
  });

  test("an opened issue is a message with its title and body", () => {
    const payload = {
      action: "opened",
      installation: { id: 99 },
      repository: { full_name: "acme/app" },
      issue: { number: 8, title: "Crash", body: "on start" },
      sender: alice,
    };
    expect(parsed(request(payload, "issues"))).toMatchObject([
      { kind: "message", address: "acme/app#8", content: "Crash\n\non start" },
    ]);
  });

  test("a PR review comment is a message on the PR", () => {
    const payload = {
      ...commentPayload("nit"),
      issue: undefined,
      pull_request: { number: 9 },
    };
    expect(
      parsed(request(payload, "pull_request_review_comment")),
    ).toMatchObject([
      { kind: "message", address: "acme/app#9", content: "nit" },
    ]);
  });

  test.each([
    [
      "the app's own comment",
      commentPayload("done", { id: 5, login: "threads-bot[bot]", type: "Bot" }),
    ],
    [
      "another bot",
      commentPayload("hi", { id: 6, login: "ci[bot]", type: "Bot" }),
    ],
    [
      "a comment carrying the effect marker",
      commentPayload("x <!-- threads:effect_key=b:c -->"),
    ],
    ["an edited comment", { ...commentPayload("hi"), action: "edited" }],
  ])("%s is ignored", (_, payload) => {
    expect(parsed(request(payload))).toEqual([{ kind: "ignore" }]);
  });

  test("another event type is ignored", () => {
    expect(parsed(request({ zen: "hi" }, "ping"))).toEqual([
      { kind: "ignore" },
    ]);
  });

  test("/approve and /deny bound to a challenge id are decisions", () => {
    expect(parsed(request(commentPayload(`/approve ${CHALLENGE}\n`)))).toEqual([
      {
        kind: "decision",
        principal,
        address: "acme/app#7",
        item_key: "d-1#0",
        challenge_id: CHALLENGE,
        decision: "grant",
      },
    ]);
    expect(parsed(request(commentPayload(`/deny ${CHALLENGE}`)))).toMatchObject(
      [{ kind: "decision", decision: "deny" }],
    );
  });

  test.each([
    "yes",
    "/approve",
    `/approve ${CHALLENGE} please`,
    `lgtm /approve ${CHALLENGE}`,
  ])("%j is a message, never a decision", (body) => {
    expect(parsed(request(commentPayload(body)))).toMatchObject([
      { kind: "message" },
    ]);
  });

  test.each([
    ["not JSON", new TextEncoder().encode("{nope")],
    [
      "a comment without a body",
      new TextEncoder().encode(
        JSON.stringify({ ...commentPayload("x"), comment: { id: 1 } }),
      ),
    ],
  ])("%s is invalid", (_, body) => {
    const result = adapter.parse({ ...request(commentPayload("x")), body });
    expect(result.ok ? "ok" : result.error.code).toBe("invalid");
  });
});

test("ack answers 200 with an empty body", () => {
  const response = adapter.ack(request(commentPayload("hi")));
  expect(response.status).toBe(200);
  expect(response.body.length).toBe(0);
});
