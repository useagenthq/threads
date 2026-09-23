import { describe, expect, test } from "bun:test";
import { type Fetch, secret, within } from "@threads/core/adapter";
import { KnownEvent } from "../../core/src/log";
import { err } from "../../core/src/result";
import type { SandboxContext } from "../../core/src/sandbox";
import { envelope, HASH } from "../../core/test/log/fixtures";
import { CTX } from "../../core/test/sandbox/context";
import { github } from "../src";
import { CHALLENGE, fakeFetch, json, TOKEN } from "./kit";

const KEY = "0192b000-0000-7000-8000-000000000001:send_4_0";
const MARKER = `<!-- threads:effect_key=${KEY} -->`;
const op = {
  kind: "comment",
  text: "Fixed.",
  address: "acme/app#7",
  installation_id: "99",
  last_inbound_at: 1790000001000,
};
const credentials = { token: TOKEN };

function make(fetch: Fetch) {
  return github({
    agent: "triage",
    webhookSecret: secret("THREADS_GH_TEST_WEBHOOK"),
    token: secret("THREADS_GH_TEST_TOKEN"),
    appSlug: "threads-bot",
    fetch,
  });
}

async function fenced<T>(
  run: () => Promise<T>,
  context: SandboxContext = CTX,
): Promise<T> {
  const done = await within(context, run);
  if (!done.ok) throw done.error.error;
  return done.value;
}

const seen: unknown[] = [];
const record = <T>(value: T): T => {
  seen.push(value);
  return value;
};

describe("render", () => {
  const adapter = make(fakeFetch(() => json(500, {})).fetch);

  test("a model response is one comment with its text", () => {
    const event = KnownEvent.parse(
      envelope("model_response", {
        request_event_id: "0192e000-0000-7000-8000-000000000001",
        content: [
          { type: "text", text: "Fixed" },
          { type: "text", text: " it." },
        ],
        stop_reason: "end_turn",
        usage: { input_tokens: 1, output_tokens: 3 },
        completeness: "complete",
      }),
    );
    expect(adapter.render(event)).toEqual([
      { kind: "comment", text: "Fixed it." },
    ]);
  });

  test("an approval request tells the approver how to answer", () => {
    const event = KnownEvent.parse(
      envelope("approval_requested", {
        challenge_id: CHALLENGE,
        call_id: "c1",
        args_hash: HASH,
        expires_at: 1790000002000,
      }),
    );
    const [rendered] = adapter.render(event);
    expect(rendered).toMatchObject({
      kind: "approval",
      challenge_id: CHALLENGE,
    });
    expect(String(rendered?.["text"])).toContain(`/approve ${CHALLENGE}`);
    expect(String(rendered?.["text"])).toContain(`/deny ${CHALLENGE}`);
  });

  test("other events render nothing", () => {
    const event = KnownEvent.parse(
      envelope(
        "user_input",
        { source: "api", text: "hi" },
        {
          actor: {
            kind: "user",
            principal: { issuer: "api", tenant: "t", subject: "s" },
          },
        },
      ),
    );
    expect(adapter.render(event)).toEqual([]);
  });
});

describe("perform", () => {
  test("a created comment is sent, with the marker, under the fence", async () => {
    const fake = fakeFetch(() => json(201, { id: 555 }));
    const outcome = record(
      await fenced(() => make(fake.fetch).perform(op, KEY, credentials)),
    );
    expect(outcome).toEqual({
      status: "sent",
      platform_ref: "acme/app#7:comment:555",
    });
    expect(fake.calls).toHaveLength(1);
    const [call] = fake.calls;
    expect(call?.url).toBe(
      "https://api.github.com/repos/acme/app/issues/7/comments",
    );
    expect(JSON.parse(String(call?.init.body))).toEqual({
      body: `Fixed.\n\n${MARKER}`,
    });
  });

  test.each([
    [
      "422",
      () => json(422, { message: "Validation Failed" }),
      "permanent",
      "definite_not_sent",
    ],
    [
      "404",
      () => json(404, { message: "Not Found" }),
      "permanent",
      "definite_not_sent",
    ],
    [
      "429",
      () => json(429, {}, { "retry-after": "5" }),
      "rate_limited",
      "definite_not_sent",
    ],
    [
      "secondary rate limit 403",
      () =>
        json(403, { message: "secondary rate limit" }, { "retry-after": "60" }),
      "rate_limited",
      "definite_not_sent",
    ],
    ["502", () => json(502, {}), "transient", "outcome_unknown"],
    [
      "connection refused",
      () => {
        throw Object.assign(new Error("refused"), { code: "ECONNREFUSED" });
      },
      "transient",
      "definite_not_sent",
    ],
    [
      "connection reset",
      () => {
        throw Object.assign(new Error("reset"), { code: "ECONNRESET" });
      },
      "transient",
      "outcome_unknown",
    ],
  ] as const)("%s → %s %s", async (_, respond, kind, sent) => {
    const fake = fakeFetch(respond);
    const outcome = record(
      await fenced(() => make(fake.fetch).perform(op, KEY, credentials)),
    );
    expect(outcome).toEqual({ status: "delivery_error", kind, sent });
    expect(fake.calls).toHaveLength(1);
  });

  test("an op that doesn't parse sends nothing", async () => {
    const fake = fakeFetch(() => json(201, { id: 1 }));
    const outcome = await fenced(() =>
      make(fake.fetch).perform({ ...op, address: "nowhere" }, KEY, credentials),
    );
    expect(outcome).toEqual({
      status: "delivery_error",
      kind: "permanent",
      sent: "definite_not_sent",
    });
    expect(fake.calls).toHaveLength(0);
  });

  test("a refusing fence sends nothing and the refusal propagates", async () => {
    const fake = fakeFetch(() => json(201, { id: 1 }));
    const refusing: SandboxContext = {
      ...CTX,
      fence: async () => err({ code: "stale_epoch", message: "lease lost" }),
    };
    const done = await within(refusing, () =>
      make(fake.fetch).perform(op, KEY, credentials),
    );
    expect(done.ok ? "sent" : done.error.stale?.code).toBe("stale_epoch");
    expect(fake.calls).toHaveLength(0);
    seen.push(done.ok ? done.value : String(done.error.error));
  });
});

describe("lookup", () => {
  const page = (bodies: readonly [number, string, string][]) =>
    bodies.map(([id, login, body]) => ({
      id,
      body,
      user: { login, type: "Bot" },
    }));

  test("finds the comment carrying the marker, across pages", async () => {
    const fake = fakeFetch((call) =>
      call.url.includes("page=2")
        ? json(200, page([[555, "threads-bot[bot]", `Fixed.\n\n${MARKER}`]]))
        : json(200, page([[1, "threads-bot[bot]", "earlier"]]), {
            link: '<https://api.github.com/repositories/1/issues/7/comments?per_page=100&page=2>; rel="next"',
          }),
    );
    const found = record(await fenced(() => make(fake.fetch).lookup(KEY, op)));
    expect(found).toEqual({ status: "found", value: "acme/app#7:comment:555" });
    expect(fake.calls[0]?.init.headers).toMatchObject({
      authorization: `token ${TOKEN}`,
    });
  });

  test("a marker forged by someone other than the app doesn't count", async () => {
    const fake = fakeFetch(() => json(200, page([[9, "mallory", MARKER]])));
    expect(await fenced(() => make(fake.fetch).lookup(KEY, op))).toEqual({
      status: "not_found_nonfinal",
    });
  });

  test("absence is nonfinal and a failure is unknown", async () => {
    const empty = fakeFetch(() => json(200, []));
    expect(await fenced(() => make(empty.fetch).lookup(KEY, op))).toEqual({
      status: "not_found_nonfinal",
    });
    const broken = fakeFetch(() => json(500, {}));
    const unknown = record(
      await fenced(() => make(broken.fetch).lookup(KEY, op)),
    );
    expect(unknown).toMatchObject({ status: "unknown" });
  });

  test("the adapter declares a nonfinal lookup", () => {
    expect(make(fakeFetch(() => json(500, {})).fetch).capabilities.lookup).toBe(
      "nonfinal",
    );
  });
});

test("the token never appears in an op, an outcome or an error", () => {
  expect(seen.length).toBeGreaterThan(5);
  expect(JSON.stringify(seen)).not.toContain(TOKEN);
  expect(JSON.stringify(op)).not.toContain(TOKEN);
});
