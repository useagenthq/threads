import { expect, test } from "bun:test";
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
