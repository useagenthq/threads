import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import type { Sinks } from "@threads/core/adapter";
import { within } from "@threads/core/adapter";
import { z } from "zod";
import { envd, envdUrl } from "../src/envd";
import { control } from "../src/rest";
import { sender } from "../src/transport";
import { Described, E2bError } from "../src/wire";

// spec/conformance/vectors/e2b-wire/cases.json replayed against this adapter's REST and envd
// clients, exactly as Python's suite replays it (tests/adapters/sandboxes/test_e2b_wire.py).
// Runtime-neutral (node:assert, no test runner), so the Bun and the Node suites both run it.

const Json = z.json();
const Headers = z.record(z.string(), z.string());
const Request = z.object({
  method: z.string(),
  url: z.string(),
  headers: Headers,
  json: Json.optional(),
  envelope: Json.optional(),
  multipart: z
    .object({ name: z.string(), filename: z.string(), text: z.string() })
    .optional(),
});
const Answer = z.object({
  status: z.int(),
  headers: Headers,
  text: z.string().optional(),
  hex: z.string().optional(),
});
const Exchange = z.object({ request: Request, response: Answer });
const Case = z.object({
  name: z.string(),
  call: z.record(z.string(), Json),
  exchanges: z.array(Exchange),
  expect: Json,
});
type Case = z.infer<typeof Case>;
const Vector = z.object({
  api_key: z.string(),
  domain: z.string(),
  envd_sandbox: Json,
  envd_urls: z.array(
    z.object({ sandbox_id: z.string(), domain: z.string(), url: z.string() }),
  ),
  cases: z.array(Case),
});

const path = new URL(
  "../../../../spec/conformance/vectors/e2b-wire/cases.json",
  import.meta.url,
);
const VECTOR = Vector.parse(JSON.parse(readFileSync(path, "utf8")));
const text = new TextDecoder();
const utf8 = new TextEncoder();

async function expectRequest(
  want: z.infer<typeof Request>,
  req: globalThis.Request,
): Promise<void> {
  assert.deepEqual([req.method, req.url], [want.method, want.url]);
  assert.equal(req.redirect, "manual", "a redirect must never be followed");
  for (const [name, value] of Object.entries(want.headers))
    assert.equal(req.headers.get(name), value, name);
  if (!req.url.startsWith(`https://api.${VECTOR.domain}`))
    for (const [, value] of req.headers)
      assert.ok(!value.includes(VECTOR.api_key), "the API key reached envd");
  if (want.multipart !== undefined) {
    const part = (await req.formData()).get(want.multipart.name);
    assert.ok(part instanceof File, "one file part");
    assert.equal(part.name, want.multipart.filename);
    assert.equal(await part.text(), want.multipart.text);
    return;
  }
  const body = new Uint8Array(await req.arrayBuffer());
  if (want.json !== undefined)
    assert.deepEqual(JSON.parse(text.decode(body)), want.json);
  else if (want.envelope !== undefined) {
    const length = new DataView(body.buffer).getUint32(1);
    assert.deepEqual([body[0], length], [0, body.length - 5]);
    assert.deepEqual(JSON.parse(text.decode(body.subarray(5))), want.envelope);
  } else assert.equal(body.length, 0);
}

function answer(given: z.infer<typeof Answer>): Response {
  const body =
    given.hex !== undefined ? Buffer.from(given.hex, "hex") : given.text;
  const empty = given.status === 204 || body === "" || body === undefined;
  return new Response(empty ? null : body, {
    status: given.status,
    headers: given.headers,
  });
}

/** A fetch that checks each request against the case's next exchange and replays its answer. */
function replayer(exchanges: readonly z.infer<typeof Exchange>[]) {
  const left = [...exchanges];
  const fetch = async (
    input: string | URL | globalThis.Request,
    init?: RequestInit,
  ) => {
    const next = left.shift();
    const req = new globalThis.Request(input, init);
    assert.ok(next !== undefined, `sent more than the vector: ${req.url}`);
    await expectRequest(next.request, req);
    return answer(next.response);
  };
  return { fetch, done: () => assert.equal(left.length, 0, "sent less") };
}

const str = (call: Case["call"], key: string): string =>
  z.string().parse(call[key]);

function sandboxOf(value: z.infer<typeof Described> | null) {
  return value === null
    ? null
    : {
        sandbox_id: value.sandboxID,
        envd_access_token: value.envdAccessToken ?? null,
        domain: value.domain ?? null,
      };
}

async function exec(
  box: ReturnType<typeof envd>,
  call: Case["call"],
): Promise<unknown> {
  const out: string[] = [];
  const err: string[] = [];
  const sinks: Sinks = {
    stdout: (b) => out.push(text.decode(b)),
    stderr: (b) => err.push(text.decode(b)),
  };
  const argv = z.tuple([z.string()], z.string()).parse(call["argv"]);
  const env = z.record(z.string(), z.string()).parse(call["env"]);
  const tag = z.string().nullable().parse(call["tag"]) ?? undefined;
  const started = await box.start(
    { argv, env, cwd: str(call, "cwd"), tag },
    sinks,
  );
  const outcome = await settled(started.exit);
  const got = { stdout: out.join(""), stderr: err.join("") };
  return "error" in outcome
    ? { ...got, exit_error: outcome.error }
    : { ...got, exit: outcome.ok };
}

async function settled<T>(
  value: Promise<T>,
): Promise<{ ok: T } | { error: string }> {
  try {
    return { ok: await value };
  } catch (error) {
    if (error instanceof E2bError) return { error: error.code };
    throw error;
  }
}

/** One case's outcome, as the vector's `expect` writes it. */
async function replay(c: Case): Promise<unknown> {
  const { fetch, done } = replayer(c.exchanges);
  const send = sender(fetch);
  const rest = control(
    send,
    `https://api.${VECTOR.domain}`,
    () => VECTOR.api_key,
  );
  const sandbox = Described.parse(VECTOR.envd_sandbox);
  const box = envd(send, envdUrl(sandbox.sandboxID, VECTOR.domain), sandbox);
  const call = c.call;
  const op = async (): Promise<unknown> => {
    switch (str(call, "op")) {
      case "create":
        return sandboxOf(
          await rest.create(str(call, "template"), str(call, "key"), {
            timeoutS: z.int().parse(call["timeout_s"]),
            internet: z.boolean().parse(call["internet"]),
          }),
        );
      case "find":
        return rest.find(str(call, "key"));
      case "describe":
        return sandboxOf(await rest.describe(str(call, "id")));
      case "kill":
        return rest.kill(str(call, "id"));
      case "start":
        return exec(box, call);
      case "signal":
        return box.signal(str(call, "tag"));
      case "upload":
        await box.upload(str(call, "path"), utf8.encode(str(call, "text")));
        return null;
      case "download":
        return text.decode(await box.download(str(call, "path")));
      default:
        throw new Error(`unknown op ${str(call, "op")}`);
    }
  };
  const fence = {
    fence: async () => ({ ok: true, value: undefined }) as const,
  };
  const ran = await within(fence, async () => settled(op()));
  // A failed request check, rethrown past the adapter.
  if (!ran.ok) throw ran.error.error;
  done();
  return "error" in ran.value ? ran.value : { ok: ran.value.ok };
}

export type Replayed = {
  readonly name: string;
  readonly expect: unknown;
  /** The case's outcome; throws when a request differs from the vector's. */
  readonly run: () => Promise<unknown>;
};

export const CASES: readonly Replayed[] = VECTOR.cases.map((c) => ({
  name: c.name,
  expect: c.expect,
  run: () => replay(c),
}));

export const ENVD_URLS: readonly {
  readonly sandbox_id: string;
  readonly domain: string;
  readonly url: string;
}[] = VECTOR.envd_urls;
