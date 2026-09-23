import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Inbound, secret } from "@threads/core/adapter";
import { github } from "../src";
import { adapter, alice, CHALLENGE, commentPayload, request } from "./kit";

const principal = { issuer: "github:99", tenant: "github:99", subject: "42" };

describe("verify", () => {
  test("a good signature yields tenant, installation and the body's hash as delivery", () => {
    const raw = request(commentPayload("hi"));
    const hash = createHash("sha256").update(raw.body).digest("hex");
    expect(adapter.verify(raw)).toEqual({
      ok: true,
      value: { tenant: "github:99", installation_id: "99", delivery_id: hash },
    });
  });

  test.each([
    ["bad signature", { "x-hub-signature-256": `sha256=${"0".repeat(64)}` }],
    ["short signature", { "x-hub-signature-256": "sha256=abcd" }],
    ["missing signature", { "x-hub-signature-256": undefined }],
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

describe("shared delivery vector", () => {
  const vector: {
    webhook_secret: string;
    event: string;
    body: string;
    signature: string;
    tenant: string;
    installation_id: string;
    delivery_id: string;
    accepted_delivery_headers: (string | null)[];
    tampered_body: string;
  } = JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../spec/conformance/vectors/github-delivery.json",
      ),
      "utf8",
    ),
  );
  process.env["THREADS_GH_VECTOR_WEBHOOK"] = vector.webhook_secret;
  const vectored = github({
    agent: "triage",
    webhookSecret: secret("THREADS_GH_VECTOR_WEBHOOK"),
    token: secret("THREADS_GH_TEST_TOKEN"),
  });
  const raw = (body: string, delivery: string | null) => ({
    headers: {
      "x-hub-signature-256": vector.signature,
      "x-github-event": vector.event,
      ...(delivery === null ? {} : { "x-github-delivery": delivery }),
    },
    body: new TextEncoder().encode(body),
  });

  test.each(vector.accepted_delivery_headers.map((h) => [h]))(
    "X-GitHub-Delivery %p is untrusted: the delivery id is the body hash",
    (header) => {
      expect(vectored.verify(raw(vector.body, header))).toEqual({
        ok: true,
        value: {
          tenant: vector.tenant,
          installation_id: vector.installation_id,
          delivery_id: vector.delivery_id,
        },
      });
    },
  );

  test("a body the signature doesn't cover is unverified", () => {
    const result = vectored.verify(raw(vector.tampered_body, "d-1"));
    expect(result.ok ? "ok" : result.error.code).toBe("unverified");
  });
});

describe("parse", () => {
  const parsed = (raw: ReturnType<typeof request>) => {
    const result = adapter.parse(raw);
    if (!result.ok) throw new Error(result.error.message);
    return result.value.map((item) => Inbound.parse(item));
  };

  test("a new comment is a message keyed by its signed body", () => {
    const raw = request(commentPayload("please look"));
    const key = createHash("sha256").update(raw.body).digest("hex");
    expect(parsed(raw)).toEqual([
      {
        kind: "message",
        principal,
        address: "acme/app#7",
        item_key: `${key}#0`,
        content: "please look",
      },
    ]);
  });

  test("a replayed body with a fresh X-GitHub-Delivery is the same item", () => {
    const payload = commentPayload("please run the deploy");
    const first = parsed(request(payload));
    const replayed = parsed(
      request(payload, "issue_comment", { "x-github-delivery": "d-replay" }),
    );
    expect(replayed).toEqual(first);
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
        item_key: expect.stringMatching(/^[0-9a-f]{64}#0$/),
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
