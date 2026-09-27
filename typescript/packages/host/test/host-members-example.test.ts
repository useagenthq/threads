import { afterEach, describe, expect, test } from "bun:test";
import { agent, scriptedModel, tool } from "@threads/core";
import { hostTeamIds, type KnownEvent } from "@threads/core/host";
import { z } from "zod";
import { answers, replyTo } from "../../core/test/team/run-kit";
import {
  alice,
  type Harness,
  harness,
  knownEventsOf,
  say,
  until,
  use,
} from "./kit";
import { assertReplays } from "./team-kit";

// examples/host-members.ts (lane 29's proof list): the example's host constructs as written, and
// the shape it describes works end to end on a scripted model — support, a caller in no team, asks
// billing, a host member; billing looks the invoice up with its own tool and replies; support's
// parked ask gets one result, the reply's text, never billing's tool output.
// Mirrors Python's tests/host/test_host_members_example.py.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

const INVOICES: ReadonlyMap<string, string> = new Map([
  ["INV-1001", "paid"],
  ["INV-1002", "overdue"],
]);

/** The example's tool, read_only, so billing needs no approvers. */
const invoiceStatus = tool({
  name: "invoice_status",
  description: "Look up an invoice's status by id, e.g. INV-1001.",
  input: z.object({ id: z.string().regex(/^INV-\d{4}$/) }),
  runs: "host",
  effect: "read_only",
  execute: async ({ id }) => INVOICES.get(id) ?? "unknown invoice",
});

/**
 * The example's billing: it looks the invoice up, then replies to the ask it took. Its reply names
 * an ask id that only exists at run time, so its answers are made from its own request.
 */
const billing = agent({
  name: "billing",
  model: answers([
    () => use("invoice_status", { id: "INV-1001" }, "b1"),
    (request) => replyTo("b2", request, "INV-1001 is paid."),
    () => say("Answered."),
  ]),
  tools: [invoiceStatus],
});

const support = agent({
  name: "support",
  model: scriptedModel({
    responses: [
      use("ask", { to: "billing", question: "Is INV-1001 paid?" }, "c1"),
      say("Billing says it is paid."),
    ],
  }),
});

const Answered = z.object({ status: z.string(), text: z.string() });

const called = (log: readonly KnownEvent[]): readonly string[] =>
  log.flatMap((e) => (e.type === "tool_call" ? [e.data.name] : []));

/** The caller's own last words: what support tells its user once billing has answered. */
function answerOf(log: readonly KnownEvent[]): string {
  const said = log.findLast((e) => e.type === "model_response");
  if (said?.type !== "model_response") return "";
  return said.data.content
    .map((part) => (part.type === "text" ? part.text : ""))
    .join("");
}

describe("the host-members example", () => {
  test("its config constructs: host() accepts it as written", async () => {
    // It calls host() at module scope, so importing it is the assertion. Catches the day a
    // refusal changes under the example (a host member that suddenly needs approvers, say).
    const example = await import("../../../examples/host-members");
    expect(example.default.channels).toEqual(["slack"]);
  });

  test("its shape answers a caller end to end", async () => {
    h = harness({
      agents: { support, billing },
      members: { billing: {} },
      messagePolicy: [{ from: "support", to: "billing", allow: ["ask"] }],
    });
    const { store } = h;
    await h.host.ready();
    const run = await h.host.startRun(
      { agent: "support", input: "Is INV-1001 paid?" },
      { principal: alice, idempotencyKey: "k-1" },
    );
    if (!run.ok) throw new Error(run.error.message);
    const branch = run.value.branch_id;
    const events = (): Promise<readonly KnownEvent[]> =>
      knownEventsOf(store, alice.tenant, branch);
    // The host's team tick materializes billing, hands it the ask and wakes the parked caller,
    // whose turn then runs on to its own answer (an ask parks a run until the answer comes back,
    // design D.5; recovery treats a resumed park as work to send, never as an interrupted turn).
    await until(
      async () => (await events()).some((e) => e.type === "turn_completed"),
      20_000,
    );
    const caller = await events();
    const ended = caller.filter((e) => e.type === "turn_completed");
    expect(ended).toHaveLength(1);
    expect(ended[0]?.type === "turn_completed" && ended[0].data.reason).toBe(
      "end_turn",
    );
    expect(answerOf(caller)).toBe("Billing says it is paid.");
    const results = caller.filter((e) => e.type === "tool_result");
    expect(results).toHaveLength(1);
    const [only] = results;
    if (only?.type !== "tool_result") throw new Error("one result");
    // The reply's text, not billing's tool output: the caller never ran invoice_status.
    expect(Answered.parse(JSON.parse(only.data.preview))).toMatchObject({
      status: "answered",
      text: "INV-1001 is paid.",
    });
    expect(called(caller)).toEqual(["ask"]);
    await assertReplays(store, alice.tenant, hostTeamIds(alice.tenant).teamId);
  }, 30_000);
});
