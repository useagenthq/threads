import { describe, expect, test } from "bun:test";
import { EVENT_TYPES, KnownEvent } from "../../src/log";
import { alice, envelope, HASH, header, kind } from "./fixtures";

describe("data schemas", () => {
  const user = { actor: { kind: "user", principal: alice } };
  test.each([
    ["user_input", { source: "api", text: "hi" }, user, "event"],
    ["user_input", { source: "api", text: "hi" }, {}, "invalid_line"],
    [
      "user_input",
      { source: "api", text: "hi", content: [{ type: "text", text: "hi" }] },
      user,
      "invalid_line",
    ],
    [
      "user_input",
      {
        source: "api",
        content: [{ type: "tool_use", call_id: "c", name: "x", input: {} }],
      },
      user,
      "invalid_line",
    ],
    [
      "user_input",
      { source: "api", text: "hi", budget: {} },
      user,
      "invalid_line",
    ],
    [
      "injected",
      {
        source: "memory",
        trust: "trusted_instruction",
        origin: { id: "m" },
        text: "x",
      },
      {},
      "invalid_line",
    ],
    [
      "injected",
      {
        source: "skill",
        trust: "trusted_instruction",
        origin: { id: "s" },
        text: "x",
      },
      {},
      "event",
    ],
    [
      "settings_changed",
      { reason: "fallback", settings: settings() },
      { actor: { kind: "model" } },
      "invalid_line",
    ],
    [
      "settings_changed",
      { reason: "user", settings: settings() },
      { actor: { kind: "user" } },
      "invalid_line",
    ],
    [
      "settings_changed",
      { reason: "user", settings: settings() },
      user,
      "event",
    ],
    ["tool_result", result("answered"), {}, "invalid_line"],
    ["tool_result", result("answered"), user, "event"],
    ["tool_result", result("executed"), {}, "event"],
    [
      "settings_changed",
      { reason: "user", settings: settings() },
      { actor: { kind: "recovery", principal: alice } },
      "invalid_line",
    ],
    [
      "settings_changed",
      { reason: "revert", settings: settings() },
      { actor: { kind: "user" } },
      "invalid_line",
    ],
    [
      "budget_exceeded",
      {
        scope: "ancestor",
        limit: "max_turns",
        limit_value: 1,
        observed: 2,
        observed_is_upper_bound: false,
      },
      {},
      "invalid_line",
    ],
    [
      "budget_exceeded",
      {
        scope: "run",
        owner_thread_id: header.thread_id,
        limit: "max_turns",
        limit_value: 1,
        observed: 2,
        observed_is_upper_bound: false,
      },
      {},
      "invalid_line",
    ],
    [
      "effect_resolved",
      { call_id: "c", outcome: "not_sent", by: "human" },
      {},
      "invalid_line",
    ],
    [
      "effect_resolved",
      { call_id: "c", outcome: "confirmed_success", by: "adapter" },
      {},
      "invalid_line",
    ],
    [
      "fork",
      { parent_branch_id: header.branch_id, at_hash: HASH, reason: "snapshot" },
      {},
      "invalid_line",
    ],
    [
      "fork",
      {
        parent_branch_id: header.branch_id,
        at_hash: HASH,
        reason: "repair",
        sandbox_id: "s",
      },
      {},
      "invalid_line",
    ],
    [
      "output_validated",
      {
        source_event_id: header.thread_id,
        schema_sha256: HASH,
        outcome: "accepted",
        value: null,
      },
      {},
      "event",
    ],
    [
      "output_validated",
      {
        source_event_id: header.thread_id,
        schema_sha256: HASH,
        outcome: "accepted",
        errors: [],
      },
      {},
      "invalid_line",
    ],
    [
      "context_edited",
      {
        reason: "threshold",
        request_event_id: header.thread_id,
        edits: [{ call_id: "c", action: "clear" }],
      },
      {},
      "invalid_line",
    ],
    [
      "context_edited",
      { reason: "manual", edits: [{ call_id: "c", action: "clear", part: 0 }] },
      {},
      "invalid_line",
    ],
    [
      "handoff",
      {
        call_id: "c",
        to_agent: "b",
        to_thread_id: header.thread_id,
        forwarded: "summary",
      },
      {},
      "invalid_line",
    ],
    [
      "tools_changed",
      { tools: [tool({ effect_class: "idempotent" })], tools_hash: HASH },
      {},
      "invalid_line",
    ],
    [
      "tools_changed",
      {
        tools: [tool({ effect_class: "idempotent", dedup_window_ms: 1000 })],
        tools_hash: HASH,
      },
      {},
      "event",
    ],
    [
      "tools_changed",
      {
        tools: [tool({ effect_class: "read_only", dedup_window_ms: 1000 })],
        tools_hash: HASH,
      },
      {},
      "invalid_line",
    ],
    [
      "model_response",
      {
        request_event_id: header.thread_id,
        content: [],
        stop_reason: "end_turn",
        usage: { input_tokens: null, output_tokens: 3 },
        completeness: "partial",
      },
      {},
      "event",
    ],
    [
      "model_response",
      {
        request_event_id: header.thread_id,
        content: [],
        stop_reason: "end_turn",
        usage: { input_tokens: 1.5, output_tokens: 3 },
        completeness: "partial",
      },
      {},
      "invalid_line",
    ],
  ])("%s %j %j → %s", (type, data, extra, want) => {
    expect(kind(envelope(type, data, extra))).toBe(want);
  });
});

function settings(): unknown {
  return {
    model: { provider: "scripted", name: "m" },
    model_params: {},
    adapter: { name: "scripted", version: "1", settings: {} },
    reasoning_carryover: "keep",
  };
}

function result(origin: string): unknown {
  return {
    call_id: "c",
    is_error: false,
    completeness: "complete",
    preview: "ok",
    origin,
  };
}

function tool(extra: Record<string, unknown>): unknown {
  return { name: "t", description: "", input_schema: {}, ...extra };
}

test("the event union covers exactly the pinned type names", () => {
  const [tagged, ...paired] = KnownEvent.options;
  const names = [
    ...tagged.options.map((o) => o.shape.type.value),
    ...paired.map((u) => u.options[0].shape.type.value),
  ];
  expect(names.toSorted()).toEqual([...EVENT_TYPES].toSorted());
});
