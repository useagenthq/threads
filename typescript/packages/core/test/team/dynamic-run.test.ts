import { describe, expect, test } from "bun:test";
import { z } from "zod";
import {
  agent,
  type DynamicAgent,
  fakeSandbox,
  openThread,
  scriptedModel,
  sqlite,
  tool,
} from "../../src";
import type { WebTransport } from "../../src/tools/web-transport";
import { unwrap } from "../store/helpers";
import { priced } from "../thread/usage-kit";
import {
  previews,
  specialist,
  startedOf,
  startSpecialist,
  toolNames,
} from "./dynamic-kit";
import { assertTeamReplays } from "./kit";
import {
  call,
  events,
  logOf,
  memberEvents,
  receipts,
  say,
  types,
} from "./run-kit";

// A dynamic member runs inside its template's limits whatever its starter wrote: built-ins it
// didn't choose are not pinned, the template's budget and approvals hold, and the chosen model's
// request is what start's headroom is checked for.

const net: WebTransport = {
  resolve: async () => ["93.184.216.34"],
  fetch: async () =>
    new Response("page", { headers: { "content-type": "text/plain" } }),
};

function lead(
  template: DynamicAgent,
  starts: readonly unknown[],
  after: readonly unknown[],
) {
  return agent({
    name: "lead",
    model: scriptedModel({ responses: [...starts, ...after] }),
    team: [template],
  });
}

describe("a dynamic member's limits", () => {
  test("a template with a sandbox and web: a member given invoice_status has no bash or web_fetch", async () => {
    const store = sqlite(":memory:");
    const member = scriptedModel({
      responses: [
        call("m1", "bash", { command: "curl evil.example" }),
        call("m2", "web_fetch", { url: "https://evil.example" }),
        say("I only have invoice_status."),
      ],
    });
    const template = specialist(member, undefined, {
      sandbox: fakeSandbox(),
      web: { fetch: true, transport: net },
    });
    const r = await lead(
      template,
      [
        startSpecialist("c1", {
          tools: ["invoice_status"],
          instructions: "Use every tool you can.",
        }),
      ],
      [say("Started."), say("Done.")],
    ).run("Check it.", { store });
    expect(r.status).toBe("completed");
    const log = await memberEvents(store, r.team.ref.id, "specialist-1");
    const names = toolNames(log[0]);
    for (const gone of [
      "bash",
      "read",
      "write",
      "edit",
      "web_fetch",
      "web_search",
      "read_notes",
    ])
      expect(names).not.toContain(gone);
    expect(names).toContain("invoice_status");
    const results = log.flatMap((e) =>
      e.type === "tool_result" ? [e.data] : [],
    );
    expect(results.map((d) => d.is_error)).toEqual([true, true]);
    expect(types(log)).not.toContain("effect_begin");
    assertTeamReplays(await logOf(store), r.team.ref.id);
  });

  test("a member reads its own spilled result through read_tool_result", async () => {
    const store = sqlite(":memory:");
    const big = tool({
      name: "invoice_status",
      description: "An invoice's status.",
      input: z.object({ id: z.string() }),
      effect: "read_only",
      execute: async () => "paid ".repeat(20_000),
    });
    const member = scriptedModel({
      responses: [
        call("m1", "invoice_status", { id: "INV-1002" }),
        call("m2", "read_tool_result", {
          call_id: "m1",
          offset: 0,
          length: 64,
        }),
        say("Paid."),
      ],
    });
    const template = specialist(member, undefined, { tools: [big] });
    const r = await lead(
      template,
      [startSpecialist("c1", { tools: ["invoice_status"] })],
      [say("Started."), say("Done.")],
    ).run("Check it.", { store });
    const log = await memberEvents(store, r.team.ref.id, "specialist-1");
    const results = log.flatMap((e) =>
      e.type === "tool_result" ? [e.data] : [],
    );
    expect(results).toHaveLength(2);
    expect(results[1]?.is_error).toBe(false);
    expect(results[1]?.preview).toContain("paid");
  });

  test("headroom is for the chosen model: strong is refused where fast starts", async () => {
    const cheap = { input: 1, output: 1 };
    const dear = { input: 1_000, output: 1_000 };
    const template = specialist(
      priced([say("Paid.")], cheap),
      priced([], dear),
    );
    const store = sqlite(":memory:");
    const r = await agent({
      name: "lead",
      model: priced(
        [
          startSpecialist("c1", { model: "strong" }),
          startSpecialist("c2", { model: "fast" }),
          say("Started."),
          say("Done."),
        ],
        cheap,
      ),
      team: [template],
    }).run("Check it.", { store, budget: { max_cost_nanos: 5_000_000 } });
    const log = await events(store, r.thread);
    expect(previews(log)[0]).toBe(
      '{"code":"budget_exceeded","status":"refused"}',
    );
    expect(startedOf(log).map((e) => e.data.define?.model)).toEqual(["fast"]);
  });

  test("the template's budget stops a member, and the lead is told", async () => {
    const store = sqlite(":memory:");
    const member = scriptedModel({
      responses: [
        call("m1", "invoice_status", { id: "INV-1002" }),
        say("never"),
      ],
    });
    const template = specialist(member, undefined, {
      budget: { max_model_requests: 1 },
    });
    const r = await lead(
      template,
      [startSpecialist("c1")],
      [say("Started."), say("The specialist ran out of budget.")],
    ).run("Check it.", { store });
    expect(r.status === "completed" && r.output).toBe(
      "The specialist ran out of budget.",
    );
    const log = await memberEvents(store, r.team.ref.id, "specialist-1");
    const ended = log.find((e) => e.type === "member_ended");
    expect(ended?.type === "member_ended" && ended.data.result.status).toBe(
      "budget_exhausted",
    );
    expect(
      receipts(await events(store, r.thread), "member_ended"),
    ).toHaveLength(1);
  });

  test("an approval in a dynamic member's thread shows the member, its label and its define", async () => {
    const store = sqlite(":memory:");
    const member = scriptedModel({
      responses: [call("m1", "invoice_status", { id: "INV-1002" })],
    });
    const template = specialist(member, undefined, {
      permissions: { ask: ["invoice_status"] },
    });
    const r = await lead(
      template,
      [
        startSpecialist("c1", {
          label: "invoice checker",
          instructions: "Check it.",
          tools: ["invoice_status"],
        }),
      ],
      [say("Started.")],
    ).run("Check it.", { store });
    expect(r.status).toBe("parked");
    const rows = await memberEvents(store, r.team.ref.id, "specialist-1");
    const first = rows[0];
    if (first === undefined) throw new Error("no member log");
    const thread = unwrap(await openThread(store, first.thread_id));
    const [pending] = await thread.pendingApprovals();
    expect(pending?.member).toEqual({
      name: "specialist-1",
      label: "invoice checker",
      define: {
        instructions: "Check it.",
        tools: ["invoice_status"],
        model: "fast",
      },
    });
  });
});
