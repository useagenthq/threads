import { expect, test } from "bun:test";
import { z } from "zod";
import { resume } from "../../src/loop";
import { ROOT, unwrap } from "../store/helpers";
import { EMAIL, events, harness, userInput } from "./harness";

// spec/schema/README.md, "Recording a response's calls": each call is recorded with its hook
// decisions and permission_decision before the next part. Python's test_hooks pins the same order.

const usage = { input_tokens: 10, output_tokens: 2 };
const send = (callId: string) => ({
  type: "tool_use",
  call_id: callId,
  name: "send_email",
  input: { to: "bob" },
});
const BOTH = {
  content: [send("call_1"), send("call_2")],
  stop_reason: "tool_use",
  usage,
};
const FINAL = {
  content: [{ type: "text", text: "Done." }],
  stop_reason: "end_turn",
  usage,
};

test("each call is recorded with its before_tool decision before the next", async () => {
  const h = harness([EMAIL], [], [BOTH, FINAL]);
  const writer = unwrap(h.store.acquire(ROOT, "owner"));
  const gate = {
    name: "guard",
    timeoutMs: 50,
    hooks: {
      before_tool: async ([call]: readonly [{ readonly call_id: string }]) =>
        call.call_id === "call_2"
          ? ({ decision: "deny", reason: "nope" } as const)
          : ({ decision: "allow" } as const),
    },
  };
  await resume(writer, h.artifacts, h.config({ extensions: [gate] }), {
    input: userInput("mail bob twice"),
  });
  const log = events(writer);
  const start = log.findIndex((e) => e.type === "model_response") + 1;
  expect(log.slice(start, start + 7).map((e) => e.type)).toEqual([
    "tool_call",
    "hook_decision",
    "permission_decision",
    "tool_call",
    "hook_decision",
    "permission_decision",
    "effect_begin",
  ]);
  expect(h.runs.get("send_email")).toBe(1);
  const denied = log.filter((e) => e.type === "tool_result")[1];
  expect(denied?.type === "tool_result" && denied.data.preview).toBe(
    "denied: nope",
  );
});

test("a crash right after a refused call's tool_call never lets it run", async () => {
  const refused = { ...send("call_1"), input: {} };
  const h = harness(
    [EMAIL],
    [],
    [{ content: [refused], stop_reason: "tool_use", usage }, FINAL],
  );
  // send_email's own schema needs `to`, so the call is refused before any effect.
  const impl = h.config().tools.get("send_email");
  if (impl === undefined) throw new Error("the harness binds send_email");
  const tools = new Map([
    ["send_email", { ...impl, input: z.strictObject({ to: z.string() }) }],
  ]);
  const first = unwrap(h.store.acquire(ROOT, "owner"));
  await resume(
    first,
    h.artifacts,
    h.config({
      tools,
      onEvent: (e) => {
        if (e.type === "tool_call") throw new Error("crash");
      },
    }),
    { input: userInput("mail nobody") },
  ).catch(() => undefined);
  h.clock.now += 60_000;
  const again = unwrap(h.store.acquire(ROOT, "owner-2"));
  await resume(again, h.artifacts, h.config({ tools }));
  const log = events(again);
  expect(h.runs.get("send_email") ?? 0).toBe(0);
  expect(log.some((e) => e.type === "permission_decision")).toBe(false);
  const result = log.find((e) => e.type === "tool_result");
  expect(result?.type === "tool_result" && result.data.origin).toBe(
    "not_executed",
  );
});

test("an ask's challenge and its park are one batch, so a crash between them can't split them", async () => {
  const h = harness([EMAIL], [], [BOTH]);
  const ask = h.config({
    authorize: () => ({ decision: "ask", source: "policy" }),
  });
  const first = unwrap(h.store.acquire(ROOT, "owner"));
  await resume(
    first,
    h.artifacts,
    {
      ...ask,
      onEvent: (e) => {
        if (e.type === "approval_requested") throw new Error("crash");
      },
    },
    { input: userInput("mail bob twice") },
  ).catch(() => undefined);
  h.clock.now += 60_000;
  const again = unwrap(h.store.acquire(ROOT, "owner-2"));
  await resume(again, h.artifacts, ask);
  const parks = events(again).filter((e) => e.type === "parked");
  expect(parks.map((e) => e.actor.kind)).toEqual(["host"]);
});
