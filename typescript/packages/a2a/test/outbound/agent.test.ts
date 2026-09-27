import { expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "@threads/core";
import { hostRunner } from "@threads/core/host";
import { logOf } from "../../../core/test/agent/kit";
import { unwrap } from "../../../core/test/store/helpers";
import { remote } from "../../src";
import { CARD_URL, partner, task } from "./partner";

// The whole path an app writes, not just the runner underneath it: `agent({tools: [...r.tools()]})`
// pins the two specs, asks for approval because a send to a partner is not read-only, routes the
// send to its own runner, and records the exchange in the thread's own log. Without this the tool
// works only when a test wires it up by hand.

const usage = { input_tokens: 10, output_tokens: 2 };
const OPERATOR = {
  issuer: "api",
  tenant: "local",
  subject: "operator",
} as const;

test("a remote's tools spread into agent({tools}), ask first, and record their exchange", async () => {
  const p = partner();
  p.send = () => ({
    kind: "task",
    task: task("task-42", "TASK_STATE_COMPLETED", "refund 42 was paid"),
  });
  const refunds = remote("refunds", CARD_URL, { transport: p, timeoutMs: 1 });
  const bot = agent({
    instructions: "Ask the partner.",
    model: scriptedModel({
      responses: [
        {
          content: [
            {
              type: "tool_use",
              call_id: "c1",
              name: "refund_desk",
              input: { message: "did refund 42 go through?" },
            },
          ],
          stop_reason: "tool_use",
          usage,
        },
        {
          content: [{ type: "text", text: "It was paid." }],
          stop_reason: "end_turn",
          usage,
        },
      ],
    }),
    tools: [
      ...refunds.tools({
        name: "refund_desk",
        description: "Ask the partner's refunds desk one question.",
      }),
    ],
  });
  const store = sqlite(":memory:");
  const parked = await bot.run("check refund 42", { store });
  // A send to a partner is not read_only, so the default permissions ask — and nothing was sent.
  expect(parked).toMatchObject({
    status: "parked",
    reason: "awaiting_approval",
  });
  expect(p.sends()).toHaveLength(0);

  const [pending] = unwrap(await parked.thread.pendingApprovals());
  if (pending === undefined) throw new Error("the send waits on approval");
  unwrap(await parked.thread.approve(pending.challenge_id, OPERATOR));
  const runner = hostRunner(bot);
  if (runner === undefined) throw new Error("agent() registers a runner");
  // Resumed, not re-asked: the approval is what the turn was waiting for.
  const result = await runner.execute(
    { store, principal: OPERATOR, thread: parked.thread },
    [],
  );
  expect(result).toMatchObject({ status: "completed", output: "It was paid." });

  const log = await logOf(result.thread);
  const types = log.map((e) => e.type);
  const started = log.find((e) => e.type === "thread_started");
  const pinned =
    started?.type === "thread_started"
      ? started.data.tools.map((t) => `${t.name}:${t.effect_class}`)
      : [];
  expect(pinned).toContain("refund_desk:reconcilable");
  expect(pinned).toContain("refund_desk_status:read_only");
  // The card and the call are durable before the begin, and the commit holds the peer's receipt.
  const at = types.indexOf("remote_card");
  expect(types.slice(at, at + 3)).toEqual([
    "remote_card",
    "remote_call",
    "effect_begin",
  ]);
  const commit = log.findLast((e) => e.type === "effect_commit");
  expect(
    commit?.type === "effect_commit" ? commit.data.provider_receipt : undefined,
  ).toBe("task-42");
  expect(types).toContain("remote_task_state");
  expect(p.sends()).toHaveLength(1);
});
