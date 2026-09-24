import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readdirSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, scriptedModel, sqlite } from "@threads/core";
import {
  openStore,
  type Principal,
  storeConnection,
  tenantStore,
  uuidv7,
} from "@threads/core/host";
import { z } from "zod";
import { uiThreadId } from "../../src/ui/key";
import {
  alice,
  bob,
  eve,
  type Harness,
  harness,
  mailer,
  say,
  use,
} from "../kit";
import { inputs } from "./e2e-kit";

// The UI routes' boundaries (spec/schema/ui/README.md): each rejected body of
// vectors/ui-inputs.json appends nothing, a chat key names one thread per principal, agent and
// tenant and is never stored, and the errors a UI route answers.

const SPEC = join(import.meta.dir, "../../../../../spec");

let h: Harness | undefined;
let dir: string | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
  if (dir !== undefined) rmSync(dir, { recursive: true, force: true });
  dir = undefined;
});

function chatBody(key: string, id: string, text: string): unknown {
  return {
    id: key,
    messages: [{ id, role: "user", parts: [{ type: "text", text }] }],
    trigger: "submit-message",
  };
}

async function post(
  on: Harness,
  as: Principal,
  body: unknown,
  agentName = "support",
): Promise<Response> {
  return on.call("POST", `/v1/ui/ai-sdk/${agentName}`, { as, body });
}

const ErrorBody = z.object({ error: z.object({ code: z.string() }) });
async function code(r: Response): Promise<string> {
  return ErrorBody.parse(await r.json()).error.code;
}

function talker(...texts: string[]) {
  return agent({
    name: "support",
    model: scriptedModel({ responses: texts.map(say) }),
  });
}

async function threadExists(
  on: Harness,
  as: Principal,
  key: string,
): Promise<boolean> {
  const { log } = await openStore(tenantStore(on.store, as.tenant));
  return log.mainBranch(uiThreadId(as, "support", key)).ok;
}

describe("vectors/ui-inputs.json", () => {
  const Row = z.object({
    name: z.string(),
    protocol: z.enum(["ai-sdk", "ag-ui"]),
    body: z.unknown(),
    code: z.string().nullable(),
  });
  const rows = z
    .array(Row)
    .parse(
      JSON.parse(
        readFileSync(join(SPEC, "conformance/vectors/ui-inputs.json"), "utf8"),
      ),
    );
  for (const row of rows)
    test(row.name, async () => {
      h = harness({ agents: { support: talker("Hi.") } });
      const r = await h.call("POST", `/v1/ui/${row.protocol}/support`, {
        as: alice,
        body: row.body,
      });
      if (row.code === null) {
        expect(r.status).toBe(200);
        await r.text();
        return;
      }
      expect(r.status).toBe(row.code === "not_found" ? 404 : 400);
      expect(await code(r)).toBe(row.code);
      for (const key of ["chat-1", "t-1"])
        expect(await threadExists(h, alice, key)).toBe(false);
    });
});

describe("chat keys", () => {
  test("one principal's key is one thread; other principals, agents and tenants get their own", async () => {
    h = harness({
      agents: {
        support: talker("One.", "Two.", "Bob's."),
        sales: talker("Sold."),
      },
    });
    await (await post(h, alice, chatBody("chat-1", "m1", "One"))).text();
    await (await post(h, alice, chatBody("chat-1", "m2", "Two"))).text();
    await (await post(h, bob, chatBody("chat-1", "m1", "Bob"))).text();
    await (
      await post(h, alice, chatBody("chat-1", "m1", "Sales"), "sales")
    ).text();
    const mine = uiThreadId(alice, "support", "chat-1");
    expect(await inputs(h.store, "acme", mine)).toHaveLength(2);
    expect(
      await inputs(h.store, "acme", uiThreadId(bob, "support", "chat-1")),
    ).toHaveLength(1);
    expect(
      await inputs(h.store, "acme", uiThreadId(alice, "sales", "chat-1")),
    ).toHaveLength(1);
    const ids = new Set([
      mine,
      uiThreadId(bob, "support", "chat-1"),
      uiThreadId(alice, "sales", "chat-1"),
      uiThreadId(eve, "support", "chat-1"),
      uiThreadId({ ...alice, tenant: "other" }, "support", "chat-1"),
    ]);
    expect(ids.size).toBe(5);
  });

  test("two concurrent first POSTs on one key: one thread, each run recorded or branch_busy", async () => {
    h = harness({ agents: { support: talker("One.", "Two.") } });
    const on = h;
    const answers = await Promise.all(
      ["m1", "m2"].map((id) => post(on, alice, chatBody("chat-1", id, id))),
    );
    const statuses = answers.map((r) => r.status);
    for (const r of answers) await r.text();
    expect(statuses.every((s) => s === 200 || s === 409)).toBe(true);
    const recorded = await inputs(
      h.store,
      "acme",
      uiThreadId(alice, "support", "chat-1"),
    );
    expect(recorded).toHaveLength(statuses.filter((s) => s === 200).length);
    expect(recorded.length).toBeGreaterThan(0);
  });

  test("no event, row or artifact holds the key's bytes", async () => {
    dir = mkdtempSync(join(tmpdir(), "threads-ui-key-"));
    const key = "chat-key-9f3c2a";
    h = harness({ store: sqlite(dir), agents: { support: talker("Hi.") } });
    await (await post(h, alice, chatBody(key, "m1", "Hello"))).text();
    await h.host.stop();
    const { db } = await storeConnection(h.store);
    db.run("PRAGMA wal_checkpoint(TRUNCATE)", []);
    const needle = Buffer.from(key);
    const files = readdirSync(dir, {
      recursive: true,
      withFileTypes: true,
    }).filter((f) => f.isFile());
    expect(files.length).toBeGreaterThan(1);
    for (const f of files)
      expect(readFileSync(join(f.parentPath, f.name)).includes(needle)).toBe(
        false,
      );
  });
});

describe("UI route errors", () => {
  test("an approval from a principal without authority is forbidden", async () => {
    h = harness({
      agents: {
        support: mailer({
          responses: [use("send_email", { to: "bob" }, "c1")],
          approvers: [bob],
        }),
      },
    });
    const opened = await h.call("POST", "/v1/ui/ag-ui/support", {
      as: alice,
      body: agUiBody([]),
    });
    const id = await interruptOf(opened);
    const r = await h.call("POST", "/v1/ui/ag-ui/support", {
      as: alice,
      body: agUiBody([
        { interruptId: id, status: "resolved", payload: { decision: "grant" } },
      ]),
    });
    expect(r.status).toBe(403);
    expect(await code(r)).toBe("forbidden");
  });

  test("an unknown run is 404; a cursor that isn't one of its frames is 400 invalid_cursor", async () => {
    h = harness({ agents: { support: talker("Hi.") } });
    await (await post(h, alice, chatBody("chat-1", "m1", "Hello"))).text();
    const thread = uiThreadId(alice, "support", "chat-1");
    const [run] = await inputs(h.store, "acme", thread);
    if (run === undefined) throw new Error("no run");
    const unknown = await h.call(
      "GET",
      `/v1/threads/${thread}/runs/${uuidv7(Date.now())}/ui/ai-sdk`,
      {
        as: alice,
      },
    );
    expect(unknown.status).toBe(404);
    for (const protocol of ["ai-sdk", "ag-ui"]) {
      const bad = await h.call(
        "GET",
        `/v1/threads/${thread}/runs/${run.event_id}/ui/${protocol}?after=999999:0`,
        { as: alice },
      );
      expect(bad.status).toBe(400);
      expect(await code(bad)).toBe("invalid_cursor");
    }
  });

  test("a POST whose last message is the assistant's creates no receipt", async () => {
    h = harness({ agents: { support: talker("Hi.") } });
    await (await post(h, alice, chatBody("chat-1", "m1", "Hello"))).text();
    const receipts = async (): Promise<readonly unknown[]> => {
      const { db } = await storeConnection(open(h).store);
      return db.all(
        "SELECT idempotency_key FROM run_receipts WHERE operation = 'ui'",
        [],
      );
    };
    const before = await receipts();
    expect(before).toHaveLength(1);
    const r = await post(h, alice, {
      id: "chat-1",
      messages: [
        { id: "m1", role: "user", parts: [{ type: "text", text: "Hello" }] },
        { id: "a1", role: "assistant", parts: [{ type: "text", text: "Hi." }] },
      ],
      trigger: "submit-message",
    });
    expect(r.status).toBe(204);
    expect(await receipts()).toEqual(before);
  });
});

function open(on: Harness | undefined): Harness {
  if (on === undefined) throw new Error("no host");
  return on;
}

const Finished = z.object({
  type: z.literal("RUN_FINISHED"),
  outcome: z.object({ interrupts: z.tuple([z.object({ id: z.string() })]) }),
});

/** The one interrupt a stream's RUN_FINISHED lists. */
async function interruptOf(r: Response): Promise<string> {
  const lines = (await r.text())
    .split("\n")
    .filter((l) => l.startsWith("data: "));
  for (const l of lines) {
    const f = Finished.safeParse(JSON.parse(l.slice("data: ".length)));
    if (f.success) return f.data.outcome.interrupts[0].id;
  }
  throw new Error("no interrupt");
}

function agUiBody(resume: readonly unknown[]): unknown {
  return {
    threadId: "t-1",
    runId: "r-1",
    state: {},
    messages: [{ id: "m1", role: "user", content: "Mail bob" }],
    tools: [],
    context: [],
    forwardedProps: {},
    ...(resume.length > 0 ? { resume } : {}),
  };
}
