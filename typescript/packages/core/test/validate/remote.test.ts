import { describe, expect, test } from "bun:test";
import type { EventDraft } from "../../src/store";
import { code, fixture, ROOT, started, THREAD, unwrap } from "../store/helpers";

// Semantic rules 56, 57 and 58 (spec/schema/README.md): a remote_call is directly followed by the
// effect_begin of its call, appears once per call_id, and a task state is observed only for a call
// whose effect committed. python/tests/reduce/test_rules.py runs the same table.

const HOST = { kind: "host" } as const;
const REQUEST = "0192e000-0000-7000-8000-000000000099";
const REF = {
  sha256: "c".repeat(64),
  bytes: 12,
  media_type: "application/json",
};

const CALL: EventDraft = {
  type: "tool_call",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: {
    call_id: "call_1",
    name: "send_message",
    input: {},
    request_event_id: REQUEST,
  },
};
const ALLOW: EventDraft = {
  type: "permission_decision",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: { call_id: "call_1", decision: "allow", source: "policy" },
};
const REMOTE: EventDraft = {
  type: "remote_call",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: {
    call_id: "call_1",
    remote: "partner",
    operation: "send_message",
    message_id: "msg-1",
    context_id: "ctx-1",
    request_ref: REF,
  },
};
const BEGIN: EventDraft = {
  type: "effect_begin",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: { call_id: "call_1", attempt: 1 },
};
const COMMIT: EventDraft = {
  type: "effect_commit",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: { call_id: "call_1", result_ref: REF },
};
const STATE: EventDraft = {
  type: "remote_task_state",
  type_version: 1,
  critical: false,
  actor: HOST,
  data: { call_id: "call_1", task_id: "task-1", state: "TASK_STATE_WORKING" },
};
const RESULT: EventDraft = {
  type: "tool_result",
  type_version: 1,
  critical: true,
  actor: HOST,
  data: {
    call_id: "call_1",
    is_error: false,
    completeness: "complete",
    preview: "sent",
    origin: "executed",
  },
};

/** Appends the drafts one by one; every draft but the last must be accepted. */
async function lastCode(drafts: readonly EventDraft[]): Promise<string> {
  const f = await fixture();
  unwrap(await f.store.createBranch(THREAD, ROOT));
  const writer = unwrap(await f.store.acquire(ROOT, "holder"));
  const all = [started, ...drafts];
  const last = all.at(-1);
  if (last === undefined) throw new Error("a draft");
  for (const d of all.slice(0, -1)) unwrap(await writer.append([d]));
  return code(await writer.append([last]));
}

const CASES: readonly [string, readonly EventDraft[], string][] = [
  // Rule 56: the effect_begin of the call follows the remote_call, and nothing else does.
  ["a begin follows the call", [CALL, ALLOW, REMOTE, BEGIN], "ok"],
  [
    "another event between them is refused",
    [CALL, ALLOW, REMOTE, RESULT],
    "invalid_transition",
  ],
  // Rule 58: one remote_call per call_id, so a resend reuses the stored request bytes.
  [
    "a second remote_call for the call is refused",
    [CALL, ALLOW, REMOTE, BEGIN, REMOTE],
    "invalid_transition",
  ],
  // Rule 57: a task state needs the call's receipt.
  [
    "a state after the commit",
    [CALL, ALLOW, REMOTE, BEGIN, COMMIT, STATE],
    "ok",
  ],
  [
    "a state before the commit is refused",
    [CALL, ALLOW, REMOTE, BEGIN, STATE],
    "invalid_transition",
  ],
  [
    "a state for a call with no remote_call is refused",
    [CALL, ALLOW, BEGIN, COMMIT, STATE],
    "invalid_transition",
  ],
];

describe("A2A semantic rules", () => {
  for (const [name, drafts, expected] of CASES)
    test(name, async () => {
      expect(await lastCode(drafts)).toBe(expected);
    });
});
