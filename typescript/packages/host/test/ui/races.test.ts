import { afterEach, describe, expect, test } from "bun:test";
import { openThread } from "@threads/core";
import {
  knownEvents,
  type Principal,
  type ThreadId,
  tenantStore,
} from "@threads/core/host";
import { HostContext } from "../../src/context";
import { applyResume } from "../../src/ui/ag-ui-resume";
import { recordParts } from "../../src/ui/ai-sdk-decisions";
import type { Log } from "../../src/ui/common";
import { uiThreadId } from "../../src/ui/key";
import { alice, bob, type Harness, harness, mailer, say, use } from "../kit";
import { pending, succeed, user } from "./ag-ui-kit";
import { agUi } from "./clients";

// A UI decision that loses a race to another approver on the REST routes
// (spec/schema/ui/README.md, "Bodies"): the approver's decision lands after the UI route read
// the log and before it records. The UI treats the interrupt as settled, as if it had read the
// log a moment later: AG-UI streams on with a resume_conflict, and the AI SDK's same decision is
// a no-op.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

/** A host context whose approval check first lets `race` run: the moment between read and record. */
class Racing extends HostContext {
  race: (() => Promise<void>) | undefined;
  override async mayApprove(
    tenant: string,
    threadId: ThreadId,
    principal: Principal,
  ): Promise<boolean> {
    const race = this.race;
    this.race = undefined;
    await race?.();
    return super.mayApprove(tenant, threadId, principal);
  }
}

const thread = uiThreadId(alice, "support", "chat-1");

/** A chat parked on one approval, the context to race it on, and the log as a UI route read it. */
async function parked(): Promise<{
  readonly on: Harness;
  readonly ctx: Racing;
  readonly id: string;
  readonly log: Log;
}> {
  const bot = mailer({
    responses: [use("send_email", { to: "bob" }, "c1"), say("Not sent.")],
    approvers: [alice, bob],
  });
  const on = harness({ agents: { support: bot } });
  h = on;
  const { agent: a } = agUi(on.host, alice, "chat-1");
  a.addMessage(user("m1", "Mail bob"));
  await succeed(a);
  const [id] = pending(a);
  if (id === undefined) throw new Error("no interrupt");
  const store = tenantStore(on.store, "acme");
  const opened = await openThread(store, thread);
  if (!opened.ok) throw new Error(opened.error.message);
  const ctx = new Racing(on.store, { support: bot }, {});
  const { log } = await ctx.open("acme");
  const read = await log.read(opened.value.branch);
  if (!read.ok) throw new Error(read.error.message);
  return {
    on,
    ctx,
    id,
    log: {
      thread: opened.value,
      events: knownEvents(read.value),
      parked: read.value.fold.parked,
    },
  };
}

function decide(on: Harness, id: string, decision: "grant" | "deny") {
  return async (): Promise<void> => {
    const r = await on.call("POST", `/v1/threads/${thread}/approvals/${id}`, {
      as: bob,
      body: { decision },
    });
    expect(r.status).toBe(200);
  };
}

describe("a UI decision that loses a race to a REST approver", () => {
  test("AG-UI: the resume streams on, with one resume_conflict", async () => {
    const { on, ctx, id, log } = await parked();
    ctx.race = decide(on, id, "deny");
    const applied = await applyResume(
      ctx,
      alice,
      log,
      [{ interruptId: id, status: "resolved", payload: { decision: "grant" } }],
      { now: Date.now(), newMessage: false },
    );
    expect(applied).toEqual({
      ok: true,
      value: [
        {
          type: "CUSTOM",
          name: "threads.resume_conflict",
          value: { interruptId: id, recorded: "denied" },
        },
      ],
    });
  });

  test("AG-UI: the same decision as the approver's is a plain no-op", async () => {
    const { on, ctx, id, log } = await parked();
    ctx.race = decide(on, id, "grant");
    const applied = await applyResume(
      ctx,
      alice,
      log,
      [{ interruptId: id, status: "resolved", payload: { decision: "grant" } }],
      { now: Date.now(), newMessage: false },
    );
    expect(applied).toEqual({ ok: true, value: [] });
  });

  test("AI SDK: the same decision as the approver's records nothing and is no error", async () => {
    const { on, ctx, id, log } = await parked();
    ctx.race = decide(on, id, "grant");
    const recorded = await recordParts(ctx, alice, log, [
      {
        type: "tool-send_email",
        toolCallId: "c1",
        state: "approval-responded",
        approval: { id, approved: true },
      },
    ]);
    expect(recorded).toEqual({ ok: true, value: [] });
  });

  test("AI SDK: another decision than the approver's is approval_duplicate, as when read after", async () => {
    const { on, ctx, id, log } = await parked();
    ctx.race = decide(on, id, "deny");
    const recorded = await recordParts(ctx, alice, log, [
      {
        type: "tool-send_email",
        toolCallId: "c1",
        state: "approval-responded",
        approval: { id, approved: true },
      },
    ]);
    expect(recorded.ok ? undefined : recorded.error.code).toBe(
      "approval_duplicate",
    );
  });

  test("AG-UI through the route: a resume sent with a REST denial always streams", async () => {
    const { on, id } = await parked();
    const [resumed] = await Promise.all([
      on.call("POST", "/v1/ui/ag-ui/support", {
        as: alice,
        body: {
          threadId: "chat-1",
          runId: "r-2",
          state: {},
          messages: [{ id: "m1", role: "user", content: "Mail bob" }],
          tools: [],
          context: [],
          forwardedProps: {},
          resume: [
            {
              interruptId: id,
              status: "resolved",
              payload: { decision: "grant" },
            },
          ],
        },
      }),
      decide(on, id, "deny")(),
    ]);
    expect(resumed.status).toBe(200);
    expect(await resumed.text()).toContain("RUN_FINISHED");
  });
});
