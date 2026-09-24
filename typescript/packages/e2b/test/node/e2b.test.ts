import assert from "node:assert/strict";
import { createServer, type IncomingMessage } from "node:http";
import { after, test } from "node:test";
import type { Fetch, SandboxContext } from "@threads/core/adapter";
import { e2b } from "../../src";

// e2b() on Node, against a fake E2B on a real socket, through Node's own fetch (undici): the
// fence holds at the real send, the key stays on the host, egress is denied by default, a
// lost create stays nonfinal, redirects aren't followed and a bad envelope is a typed error.

const KEY = "e2b_node_key_canary";
const TOKEN = "envd-node-token";
const DOMAIN = "e2b.test";

type Hit = {
  readonly method: string;
  readonly target: string;
  readonly headers: IncomingMessage["headers"];
  readonly body: string;
};
type Reply = {
  status: number;
  headers?: Record<string, string>;
  body?: Uint8Array | string;
};
/** A route's answer; "drop" closes the connection unanswered. */
type Route = (hit: Hit) => Reply | "drop" | undefined;

const utf8 = new TextEncoder();
function envelope(flags: number, message: unknown): Uint8Array {
  const body = utf8.encode(JSON.stringify(message));
  const out = new Uint8Array(5 + body.length);
  out[0] = flags;
  new DataView(out.buffer).setUint32(1, body.length);
  out.set(body, 5);
  return out;
}
const concat = (...parts: Uint8Array[]) => Buffer.concat(parts);
const sandboxJson = (id: string) =>
  JSON.stringify({
    sandboxID: id,
    envdVersion: "0.5.8",
    envdAccessToken: TOKEN,
    domain: DOMAIN,
  });
const started = concat(
  envelope(0, { event: { start: { pid: 1 } } }),
  envelope(0, { event: { end: { exited: true } } }),
  envelope(2, {}),
);

/** E2B as a happy provider; `route` answers first when it has an answer. */
const standard: Route = ({ method, target }) => {
  if (method === "POST" && target === `api.${DOMAIN}/v2/sandboxes`)
    return {
      status: 201,
      headers: { "content-type": "application/json" },
      body: sandboxJson("sbx_n"),
    };
  if (method === "GET" && target.startsWith(`api.${DOMAIN}/v2/sandboxes?`))
    return {
      status: 200,
      headers: { "content-type": "application/json" },
      body: "[]",
    };
  if (target.endsWith("/process.Process/Start"))
    return {
      status: 200,
      headers: { "content-type": "application/connect+json" },
      body: started,
    };
  return undefined;
};

const servers: (() => void)[] = [];
after(() => {
  for (const close of servers) close();
});

/** A fake E2B on 127.0.0.1, and the fetch that reaches it for any E2B host. */
async function fake(route: Route = () => undefined) {
  const hits: Hit[] = [];
  const server = createServer(async (req, res) => {
    const chunks: Buffer[] = [];
    for await (const chunk of req) chunks.push(Buffer.from(chunk));
    const hit: Hit = {
      method: req.method ?? "",
      target: (req.url ?? "").slice(1),
      headers: req.headers,
      body: Buffer.concat(chunks).toString(),
    };
    hits.push(hit);
    const reply = route(hit) ?? standard(hit) ?? { status: 404, body: "{}" };
    if (reply === "drop") req.socket.destroy();
    else res.writeHead(reply.status, reply.headers).end(reply.body);
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  servers.push(() => server.close());
  const address = server.address();
  if (address === null || typeof address === "string")
    throw new Error("no port");
  const origin = `http://127.0.0.1:${address.port}`;
  const fetch = (input: string | URL | Request, init?: RequestInit) => {
    const url = new URL(input instanceof Request ? input.url : input);
    return globalThis.fetch(
      `${origin}/${url.host}${url.pathname}${url.search}`,
      init,
    );
  };
  return { hits, fetch, origin };
}

const context = (live: () => boolean): SandboxContext => ({
  authority: { kind: "cleanup", resource_id: "r", claim: "c" },
  fence: async () =>
    live()
      ? { ok: true, value: undefined }
      : { ok: false, error: { code: "stale_epoch", message: "lease lost" } },
});
const OPEN = context(() => true);

const adapter = (fetch: Fetch, allowInternet = false) =>
  e2b({
    apiKey: KEY,
    domain: DOMAIN,
    fetch,
    ...(allowInternet ? { allowInternet } : {}),
  });

test("a stale owner sends nothing: the fake E2B receives no request", async () => {
  const { hits, fetch } = await fake();
  const made = await adapter(fetch).create(
    "op-stale",
    context(() => false),
  );
  assert.equal(made.ok ? "created" : made.error.code, "stale_epoch");
  assert.deepEqual(hits, []);
});

test("a lease lost after the create: the create reached E2B, nothing after it left", async () => {
  const { hits, fetch } = await fake();
  let left = 1;
  const made = await adapter(fetch).create(
    "op-lost-lease",
    context(() => left-- > 0),
  );
  assert.equal(made.ok ? "created" : made.error.code, "stale_epoch");
  assert.deepEqual(
    hits.map((h) => h.target),
    [`api.${DOMAIN}/v2/sandboxes`],
  );
});

test("the API key authenticates the control plane only; envd gets the sandbox's token", async () => {
  const { hits, fetch } = await fake();
  const made = await adapter(fetch).create("op-key", OPEN);
  assert.ok(made.ok);
  const [create, ...envd] = hits;
  assert.equal(create?.headers["x-api-key"], KEY);
  assert.ok(envd.length > 0);
  for (const hit of envd) {
    assert.ok(hit.target.startsWith(`49983-sbx_n.${DOMAIN}/`));
    assert.equal(hit.headers["x-access-token"], TOKEN);
    assert.ok(!JSON.stringify(hit).includes(KEY), "the API key reached envd");
  }
});

test("egress is denied by default, and declared unenforced only when opened", async () => {
  const { hits, fetch } = await fake();
  const closed = adapter(fetch);
  assert.equal(closed.info.egress, "enforced");
  assert.ok((await closed.create("op-egress", OPEN)).ok);
  assert.equal(JSON.parse(hits[0]?.body ?? "{}").allow_internet_access, false);
  assert.equal(adapter(fetch, true).info.egress, "unenforced");
});

test("a lost create is unavailable, and its lookup finds nothing only nonfinally", async () => {
  const { hits, fetch } = await fake((hit) =>
    hit.method === "POST" ? "drop" : undefined,
  );
  const sandbox = adapter(fetch);
  const made = await sandbox.create("op-lost", OPEN);
  assert.equal(made.ok ? "created" : made.error.code, "unavailable");
  const looked = await sandbox.lookup?.("op-lost", OPEN);
  assert.equal(looked?.ok && looked.value.status, "not_found_nonfinal");
  assert.equal(sandbox.info.lookup.create, "nonfinal");
  assert.ok(
    hits[1]?.target.includes("metadata=threads_operation_key%3Dop-lost"),
  );
});

test("a redirect is not followed: its target receives nothing", async () => {
  const { hits, fetch, origin } = await fake((hit) =>
    hit.method === "POST"
      ? {
          status: 307,
          headers: { location: `${origin}/api.${DOMAIN}/elsewhere` },
        }
      : undefined,
  );
  const made = await adapter(fetch).create("op-redirect", OPEN);
  assert.equal(made.ok ? "created" : made.error.code, "unavailable");
  assert.deepEqual(
    hits.map((h) => h.target),
    [`api.${DOMAIN}/v2/sandboxes`],
  );
});

test("an oversize envd envelope from a real socket is a typed error, not a crash", async () => {
  const header = new Uint8Array(5);
  new DataView(header.buffer).setUint32(1, 4 * 1024 * 1024 + 1);
  const { fetch } = await fake((hit) =>
    hit.target.endsWith("/process.Process/Start")
      ? {
          status: 200,
          headers: { "content-type": "application/connect+json" },
          body: header,
        }
      : undefined,
  );
  const made = await adapter(fetch).create("op-oversize", OPEN);
  assert.equal(made.ok ? "created" : made.error.code, "unavailable");
});

// Not in the shared vector: Python's connectrpc client ignores flag bits it doesn't know.
test("an envd envelope with an unknown flag bit is a typed error", async () => {
  const unknown = envelope(0x80, { event: { start: { pid: 1 } } });
  const { fetch } = await fake((hit) =>
    hit.target.endsWith("/process.Process/Start")
      ? {
          status: 200,
          headers: { "content-type": "application/connect+json" },
          body: unknown,
        }
      : undefined,
  );
  const made = await adapter(fetch).create("op-flag", OPEN);
  assert.equal(made.ok ? "created" : made.error.code, "unavailable");
});

test("a lookup follows the page token and finds its sandbox on page two", async () => {
  const listed = (key: string) => ({
    sandboxID: `sbx-${key}`,
    envdVersion: "0.5.8",
    metadata: { threads_operation_key: key },
  });
  const { hits, fetch } = await fake((hit) => {
    if (!hit.target.startsWith(`api.${DOMAIN}/v2/sandboxes?`)) return undefined;
    const second = hit.target.includes("nextToken=page-2");
    return {
      status: 200,
      headers: {
        "content-type": "application/json",
        ...(second ? {} : { "x-next-token": "page-2" }),
      },
      body: JSON.stringify([listed(second ? "op-paged" : "someone-else")]),
    };
  });
  const found = await adapter(fetch).lookup?.("op-paged", OPEN);
  assert.equal(found?.ok && found.value.status, "found");
  assert.equal(hits.length, 2);
});
