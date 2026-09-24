import { afterEach, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { uiThreadId } from "../../src/ui/key";
import { alice, bob, say, use } from "../kit";
import {
  attempt,
  pending,
  succeed,
  uninterrupted,
  user,
} from "../ui/ag-ui-kit";
import { agUi, chat, type Fetcher, settled } from "../ui/clients";
import { aiSdkUninterrupted, recordedRuns } from "../ui/e2e-kit";
import { agUiProjection, aiSdkProjection } from "../ui/stock";

// The stock AI SDK and AG-UI clients against the Python host (python/tests/host/ui_server.py),
// served on a local port: the same client steps as the in-process end-to-end tests, and the
// same equality with an uninterrupted client. Skipped where python/.venv isn't set up (the
// Python workflow runs it).

const PYTHON = join(import.meta.dir, "../../../../../python");
const BIN = join(PYTHON, ".venv/bin/python");

const running: Bun.Subprocess[] = [];
afterEach(() => {
  for (const p of running.splice(0)) p.kill();
});

async function pythonHost(responses: readonly unknown[]): Promise<Fetcher> {
  const proc = Bun.spawn(
    [BIN, "tests/host/ui_server.py", JSON.stringify({ responses })],
    { cwd: PYTHON, stdout: "pipe", stderr: "inherit" },
  );
  running.push(proc);
  let out = "";
  for await (const chunk of proc.stdout) {
    out += new TextDecoder().decode(chunk);
    if (out.includes("\n")) break;
  }
  const port = z.coerce.number().int().parse(out.trim());
  const base = `http://127.0.0.1:${port}`;
  return {
    fetch: async (r) =>
      fetch(r.url.replace("http://host.test", base), {
        method: r.method,
        headers: r.headers,
        ...(r.method === "GET" ? {} : { body: await r.text() }),
      }),
  };
}

const Assistant = z.object({ id: z.string() });
const mail = user("m1", "Mail bob");
const grant = { status: "resolved", payload: { decision: "grant" } } as const;
const lookup = [use("lookup", { q: "answer" }, "c1"), say("It is 42.")];

function only(ids: readonly string[]): string {
  const [id, ...more] = ids;
  if (id === undefined || more.length > 0) throw new Error("not one interrupt");
  return id;
}

describe.skipIf(!existsSync(BIN))(
  "the stock clients against the Python host",
  () => {
    test("AI SDK: a message, then an approval that sends automatically", async () => {
      const h = await pythonHost([
        say("Hi there."),
        use("send_email", { to: "bob" }, "c1"),
        say("Mailed."),
      ]);
      const { chat: c, log } = chat(h, alice, "chat-1");
      await c.sendMessage({ text: "Hello" });
      await settled(c);
      const thread = uiThreadId(alice, "support", "chat-1");
      const first = Assistant.parse(c.messages.at(-1)).id;
      const want = async (run: string) =>
        aiSdkProjection(
          z
            .custom<Parameters<typeof aiSdkProjection>[0]>()
            .parse(await aiSdkUninterrupted(h, alice, thread, run)),
        );
      expect(aiSdkProjection(c.messages.at(-1))).toEqual(await want(first));
      await c.sendMessage({ text: "Mail bob" });
      await settled(c);
      const part = z
        .object({ approval: z.object({ id: z.string() }) })
        .parse(c.messages.at(-1)?.parts.find((p) => "approval" in p));
      await c.addToolApprovalResponse({ id: part.approval.id, approved: true });
      await settled(c, () => JSON.stringify(c.messages).includes("Mailed."));
      const second = Assistant.parse(c.messages.at(-1)).id;
      expect(aiSdkProjection(c.messages.at(-1))).toEqual(await want(second));
      expect(c.messages).toHaveLength(4);
      await c.resumeStream();
      expect(log.requests.at(-1)?.method).toBe("GET");
      expect(c.status).toBe("ready");
      expect(await recordedRuns(h, alice, thread)).toHaveLength(2);
    });

    test("AG-UI: an interrupt resumed with a grant, then a message resumed with cancelled", async () => {
      const h = await pythonHost([
        use("send_email", { to: "bob" }, "c1"),
        say("Mailed."),
        use("send_email", { to: "carol" }, "c2"),
        say("Not sent."),
      ]);
      const { agent: a } = agUi(h, alice, "chat-1");
      a.addMessage(mail);
      await succeed(a);
      await succeed(a, {
        resume: [{ interruptId: only(pending(a)), ...grant }],
      });
      const again = user("m2", "Mail carol");
      a.addMessage(again);
      await succeed(a);
      await succeed(a, {
        resume: [{ interruptId: only(pending(a)), status: "cancelled" }],
      });
      expect(pending(a)).toEqual([]);
      expect(agUiProjection(a.messages)).toEqual(
        await uninterrupted(h, alice, "chat-1", [mail, again]),
      );
    });

    for (const n of [3, 7])
      test(`AG-UI: dropped after frame ${n}, the retry replays the one run`, async () => {
        const h = await pythonHost(lookup);
        const { agent: a, log } = agUi(h, alice, "chat-1");
        const ask = user("m1", "What is it?");
        a.addMessage(ask);
        log.dropAfter = n;
        const dropped = await attempt(a);
        expect(dropped.events.at(-1)?.type).not.toBe("RUN_FINISHED");
        await succeed(a);
        const thread = uiThreadId(alice, "support", "chat-1");
        expect(await recordedRuns(h, alice, thread)).toHaveLength(1);
        expect(agUiProjection(a.messages)).toEqual(
          await uninterrupted(h, alice, "chat-1", [ask]),
        );
      });

    test("AG-UI: another approver denied first, so the grant is a resume_conflict", async () => {
      const h = await pythonHost([
        use("send_email", { to: "bob" }, "c1"),
        say("Not sent."),
      ]);
      const { agent: a } = agUi(h, alice, "chat-1");
      a.addMessage(mail);
      await succeed(a);
      const id = only(pending(a));
      const thread = uiThreadId(alice, "support", "chat-1");
      const denied = await h.fetch(
        new Request(`http://host.test/v1/threads/${thread}/approvals/${id}`, {
          method: "POST",
          headers: {
            "x-principal": JSON.stringify(bob),
            "content-type": "application/json",
          },
          body: JSON.stringify({ decision: "deny" }),
        }),
      );
      expect(denied.status).toBe(200);
      const events = await succeed(a, {
        resume: [{ interruptId: id, ...grant }],
      });
      expect<unknown[]>(events.filter((e) => e.type === "CUSTOM")).toEqual([
        {
          type: "CUSTOM",
          name: "threads.resume_conflict",
          value: { interruptId: id, recorded: "denied" },
        },
      ]);
      expect(pending(a)).toEqual([]);
    });
  },
);
