import { afterEach, describe, expect, setSystemTime, test } from "bun:test";
import type { HttpAgent } from "@ag-ui/client";
import { agent } from "@threads/core";
import { z } from "zod";
import { uiThreadId } from "../../src/ui/key";
import { alice, bob, type Harness, harness, mailer, say, use } from "../kit";
import { attempt, pending, succeed, uninterrupted, user } from "./ag-ui-kit";
import { agUi } from "./clients";
import { inputs, liveModel } from "./e2e-kit";
import { agUiProjection } from "./stock";

// The stock AG-UI 1.0 client (HttpAgent) against the host: a run, an interrupt answered with a
// grant or `cancelled`, an expired approval, a resumed run that errors, and another approver
// settling an interrupt first. Every stream passes the client's own verifier (runAgent would
// reject), and agent.messages equals an uninterrupted client's.

let h: Harness | undefined;
afterEach(async () => {
  setSystemTime();
  await h?.host.stop();
  h = undefined;
});

function open(): Harness {
  if (h === undefined) throw new Error("no host");
  return h;
}

async function equalsUninterrupted(
  a: HttpAgent,
  users: Parameters<typeof uninterrupted>[3],
): Promise<void> {
  expect(agUiProjection(a.messages)).toEqual(
    await uninterrupted(open().host, alice, "chat-1", users),
  );
}

function only(a: HttpAgent): string {
  const [id, ...more] = pending(a);
  if (id === undefined || more.length > 0) throw new Error("not one interrupt");
  return id;
}

const mail = user("m1", "Mail bob");
const grant = { status: "resolved", payload: { decision: "grant" } } as const;

function mailing(responses: readonly unknown[], sent: string[]): Harness {
  return harness({
    agents: {
      support: mailer({
        responses: [use("send_email", { to: "bob" }, "c1"), ...responses],
        approvers: [alice, bob],
        sent,
      }),
    },
  });
}

describe("AG-UI end to end", () => {
  test("a run", async () => {
    h = harness({
      agents: {
        support: agent({
          name: "support",
          model: liveModel([say("Hi there, Alice.")]),
        }),
      },
    });
    const { agent: a } = agUi(h.host, alice, "chat-1");
    const hello = user("m1", "Hello");
    a.addMessage(hello);
    await succeed(a);
    expect(a.messages.map((m) => m.role)).toEqual(["user", "assistant"]);
    await equalsUninterrupted(a, [hello]);
    const thread = uiThreadId(alice, "support", "chat-1");
    const recorded = await inputs(h.store, "acme", thread);
    expect(recorded.map((e) => e.data.client_message_id)).toEqual(["m1"]);
  });

  test("an interrupt, then a resume with a grant", async () => {
    const sent: string[] = [];
    h = mailing([say("Mailed.")], sent);
    const { agent: a } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    expect(sent).toEqual([]);
    await succeed(a, { resume: [{ interruptId: only(a), ...grant }] });
    expect(sent).toEqual(["bob"]);
    expect(pending(a)).toEqual([]);
    await equalsUninterrupted(a, [mail]);
  });

  test("a question (ask_user), answered by a resume with an Answer", async () => {
    h = harness({
      agents: {
        support: agent({
          name: "support",
          model: liveModel([
            use(
              "ask_user",
              { question: "Which colour?", options: ["Red", "Blue"] },
              "q1",
            ),
            say("Blue it is."),
          ]),
        }),
      },
    });
    const { agent: a } = agUi(h.host, alice, "chat-1");
    const paint = user("m1", "Paint it");
    a.addMessage(paint);
    const asked = await succeed(a);
    expect(
      z
        .object({
          outcome: z.object({
            interrupts: z.array(z.object({ reason: z.string() })),
          }),
        })
        .parse(asked.at(-1))
        .outcome.interrupts.map((i) => i.reason),
    ).toEqual(["user_input"]);
    await succeed(a, {
      resume: [
        {
          interruptId: only(a),
          status: "resolved",
          payload: { answer: "Blue" },
        },
      ],
    });
    expect(pending(a)).toEqual([]);
    expect(JSON.stringify(a.messages)).toContain("Blue it is.");
    await equalsUninterrupted(a, [paint]);
  });

  test("a resume with cancelled denies", async () => {
    const sent: string[] = [];
    h = mailing([say("Not sent.")], sent);
    const { agent: a } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    await succeed(a, {
      resume: [{ interruptId: only(a), status: "cancelled" }],
    });
    expect(sent).toEqual([]);
    expect(pending(a)).toEqual([]);
    await equalsUninterrupted(a, [mail]);
  });

  test("an expired approval: cancelled ends in RUN_FINISHED, and the next message runs", async () => {
    const sent: string[] = [];
    h = mailing([say("Later.")], sent);
    const { agent: a } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    const id = only(a);
    setSystemTime(Date.now() + 2 * 24 * 60 * 60 * 1000);
    const events = await succeed(a, {
      resume: [{ interruptId: id, status: "cancelled" }],
    });
    expect<string | undefined>(events.at(-1)?.type).toBe("RUN_FINISHED");
    expect(pending(a)).toEqual([]);
    const next = user("m2", "Try again later");
    a.addMessage(next);
    await succeed(a);
    expect(sent).toEqual([]);
    await equalsUninterrupted(a, [mail, next]);
  });

  test("a resumed run that errors: the next message, with the settled resume, runs", async () => {
    const sent: string[] = [];
    const tooLong = { error: { reason: "prompt_too_long", http_status: 400 } };
    h = mailing([tooLong, say("Yes.")], sent);
    const { agent: a } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    const id = only(a);
    const errored = await succeed(a, {
      resume: [{ interruptId: id, ...grant }],
    });
    expect<string | undefined>(errored.at(-1)?.type).toBe("RUN_ERROR");
    expect(pending(a)).toEqual([id]);
    const next = user("m2", "Are you there?");
    a.addMessage(next);
    expect((await attempt(a)).error).toContain("not addressed by resume");
    const events = await succeed(a, {
      resume: [{ interruptId: id, ...grant }],
    });
    expect<string | undefined>(events.at(-1)?.type).toBe("RUN_FINISHED");
    expect<unknown[]>(events.filter((e) => e.type === "CUSTOM")).toEqual([]);
    expect(pending(a)).toEqual([]);
    expect(sent).toEqual(["bob"]);
    await equalsUninterrupted(a, [mail, next]);
  });

  test("another approver denied first: one resume_conflict, then RUN_FINISHED", async () => {
    const sent: string[] = [];
    h = mailing([say("Not sent."), say("Hello.")], sent);
    const { agent: a } = agUi(h.host, alice, "chat-1");
    a.addMessage(mail);
    await succeed(a);
    const id = only(a);
    const thread = uiThreadId(alice, "support", "chat-1");
    const denied = await h.call(
      "POST",
      `/v1/threads/${thread}/approvals/${id}`,
      { as: bob, body: { decision: "deny" } },
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
    expect<string | undefined>(events.at(-1)?.type).toBe("RUN_FINISHED");
    expect(pending(a)).toEqual([]);
    expect(sent).toEqual([]);
    await equalsUninterrupted(a, [mail]);
    const next = user("m2", "Hi");
    a.addMessage(next);
    await succeed(a);
    await equalsUninterrupted(a, [mail, next]);
  });
});
