import { afterEach, expect, test } from "bun:test";
import { textOf as partsText } from "@threads/a2a/protocol";
import { z } from "zod";
import { a2aThreadId } from "../../src/a2a/keys";
import { taskOf } from "../../src/a2a/state";
import type { RunOutcome } from "../../src/outcome";
import { alice, say, use } from "../kit";
import { actor } from "./agents";
import { message, reaches, serve, stopAll, task } from "./kit";

// A parked run is in-doubt work, and it is visible rather than hidden: it shows WORKING and says
// what it waits for. Never FAILED, because we do not know it failed, and never COMPLETED, because we
// do not know it worked. A person resolves it through threads, and a partner is never an approver:
// there is no A2A operation that decides one of our parks.

afterEach(stopAll);

const ONE = { support: { description: "Support." } };

function textOf(t: Awaited<ReturnType<typeof task>>): string {
  return partsText(t.status.message?.parts ?? []);
}

/** Polls GetTask until the status message says `text`, reporting what it said instead. */
async function says(
  on: Awaited<ReturnType<typeof serve>>,
  taskId: string,
  text: string,
  ms = 10_000,
): Promise<Awaited<ReturnType<typeof task>>> {
  const deadline = Date.now() + ms;
  const seen: string[] = [];
  for (;;) {
    const now = await task(
      await on.rpc("GetTask", { id: taskId }, { as: alice }),
    );
    const said = `${now.status.state} ${textOf(now)}`.trim();
    if (seen.at(-1) !== said) seen.push(said);
    if (textOf(now) === text) return now;
    if (Date.now() > deadline)
      throw new Error(
        `task ${taskId} never said ${JSON.stringify(text)}; it said ${seen.join(" → ")}`,
      );
    await Bun.sleep(20);
  }
}

test("an approval park shows WORKING, and only an approver acting through threads moves it on", async () => {
  const on = await serve({
    agents: {
      support: actor({
        responses: [use("refund", { id: "inv-1" }, "c1"), say("refunded")],
        approvers: [alice],
      }),
    },
    a2a: { expose: ONE },
  });
  const sent = await task(
    await on.rpc(
      "SendMessage",
      message("m1", "refund inv-1", { contextId: "c" }),
      {
        as: alice,
      },
    ),
  );
  const waiting = await says(on, sent.id, "waiting for approval");
  expect(waiting.status.state).toBe("TASK_STATE_WORKING");
  // A2A offers no way to decide it: the caller's only operations are send, get, list and cancel.
  const thread = a2aThreadId(alice, "support", "c");
  const pending = z
    .array(z.object({ challenge_id: z.string() }))
    .parse(
      await (
        await on.raw("GET", `/v1/threads/${thread}/approvals`, { as: alice })
      ).json(),
    );
  expect(pending).toHaveLength(1);
  const granted = await on.raw(
    "POST",
    `/v1/threads/${thread}/approvals/${pending[0]?.challenge_id}`,
    { as: alice, body: { decision: "grant" } },
  );
  expect(granted.status).toBe(200);
  // Approved through threads, the exposed task carries on and finishes.
  const done = await reaches(on, alice, sent.id, ["TASK_STATE_COMPLETED"]);
  expect(done.artifacts?.[0]?.parts).toEqual([{ text: "refunded" }]);
});

// The other park reasons cannot be reached from an exposed run with a scripted model: an app tool's
// throw is deliberately an error result the model sees, not uncertainty, and the reasons that do
// park (a provider that went unavailable, a resource, a member) need machinery an exposed agent has
// no way to configure. So the pure function every route answers from is driven directly instead.

const taskId = z
  .string()
  .brand<"EventId">()
  .parse("01a00000-0000-7000-8000-000000000000");

const parkedAs = (reason: string): RunOutcome =>
  ({
    thread_id: "t",
    branch_id: "b",
    status: "parked",
    reason,
    pending: [],
  }) as unknown as RunOutcome;

const parked = (reason: string) =>
  taskOf({
    taskId,
    contextId: "c",
    own: [],
    outcome: parkedAs(reason),
    question: undefined,
  }).status;

test("an uncertain effect shows WORKING and asks for a person, not a partner", async () => {
  const status = parked("effect_unknown");
  expect(status.state).toBe("TASK_STATE_WORKING");
  expect(status.message?.parts).toEqual([
    { text: "waiting for a person to resolve an uncertain action" },
  ]);
});

test("every park reason shows WORKING and says what it waits for", async () => {
  for (const reason of [
    "awaiting_approval",
    "effect_unknown",
    "awaiting_resource",
    "awaiting_member",
    "awaiting_input",
  ]) {
    const status = parked(reason);
    expect(status.state).not.toBe("TASK_STATE_FAILED");
    expect(status.state).not.toBe("TASK_STATE_COMPLETED");
    expect(partsText(status.message?.parts ?? [])).not.toBe("");
  }
});

test("a park reason with no wording of its own is reported as itself", async () => {
  expect(parked("awaiting_resource").message?.parts).toEqual([
    { text: "awaiting_resource" },
  ]);
  expect(parked("awaiting_member").message?.parts).toEqual([
    { text: "awaiting_member" },
  ]);
});
