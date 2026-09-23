import { describe, expect, test } from "bun:test";
import type { KnownEvent } from "../../src/log";
import { resume } from "../../src/loop";
import type { EventDraft } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { EMAIL, events, harness, userInput } from "./harness";

// Invariants 2 and 3 (AGENTS.md): a stale owner never reaches an adapter, and a settled
// effect is never dispatched again. repros.

const FINAL = {
  content: [{ type: "text", text: "Done." }],
  stop_reason: "end_turn",
  usage: { input_tokens: 10, output_tokens: 2 },
};
const SEND = {
  content: [
    {
      type: "tool_use",
      call_id: "call_1",
      name: "send_email",
      input: { to: "bob" },
    },
  ],
  stop_reason: "tool_use",
  usage: { input_tokens: 10, output_tokens: 2 },
};

/** Another owner takes the expired lease right after `type` commits. */
function takeoverAfter(
  h: ReturnType<typeof harness>,
  type: KnownEvent["type"],
) {
  return (e: KnownEvent): void => {
    if (e.type !== type) return;
    h.clock.now += 60_000;
    unwrap(h.store.acquire(ROOT, "usurper"));
  };
}

describe("a stale owner never dispatches (invariant 2)", () => {
  test("model.send is not called after a takeover following model_request", async () => {
    const h = harness([], [userInput("hi")], [FINAL]);
    const writer = unwrap(h.store.acquire(ROOT, "owner", 30_000));
    const end = await resume(
      writer,
      h.artifacts,
      h.config({
        onEvent: takeoverAfter(h, "model_request"),
      }),
    );
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "branch_busy" },
    });
    expect(h.model.remaining()).toBe(1);
  });

  test("tool.run is not called after a takeover following effect_begin", async () => {
    const h = harness([EMAIL], [userInput("mail bob")], [SEND, FINAL]);
    const writer = unwrap(h.store.acquire(ROOT, "owner", 30_000));
    const end = await resume(
      writer,
      h.artifacts,
      h.config({
        onEvent: takeoverAfter(h, "effect_begin"),
      }),
    );
    expect(end).toMatchObject({
      kind: "halted",
      halt: { code: "branch_busy" },
    });
    expect(h.runs.get("send_email") ?? 0).toBe(0);
    expect(events(writer).at(-1)?.type).toBe("effect_begin");
  });

  test("a second owner's appends are refused while the first holds the lease", () => {
    const h = harness([], [userInput("hi")], []);
    unwrap(h.store.acquire(ROOT, "owner", 30_000));
    expect(h.store.acquire(ROOT, "other").ok).toBe(false);
  });
});

const host = { kind: "host" } as const;
const recovery = { kind: "recovery" } as const;

/** A crashed call: model_request, the tool_use response, the call, then `effects`. */
function crashedCall(
  h: ReturnType<typeof harness>,
  effects: readonly EventDraft[],
): void {
  const writer = unwrap(h.store.acquire(ROOT, "crashed", 1));
  const [request] = unwrap(
    writer.append([
      {
        type: "model_request",
        type_version: 1,
        critical: true,
        actor: host,
        data: {
          attempt: 1,
          request_ref: {
            sha256: "b".repeat(64),
            bytes: 1,
            media_type: "application/x-ndjson",
          },
          declared_prefix: { bytes: 1, sha256: "c".repeat(64) },
        },
      },
    ]),
  );
  const request_event_id = request?.event.event_id ?? "";
  const call = { call_id: "call_1", name: "send_email", input: { to: "bob" } };
  unwrap(
    writer.append([
      {
        type: "model_response",
        type_version: 1,
        critical: true,
        actor: { kind: "model" },
        data: {
          request_event_id,
          content: [{ type: "tool_use", ...call }],
          stop_reason: "tool_use",
          usage: { input_tokens: 1, output_tokens: 1 },
          completeness: "complete",
        },
      },
      {
        type: "tool_call",
        type_version: 1,
        critical: true,
        actor: host,
        data: { ...call, request_event_id },
      },
      {
        type: "permission_decision",
        type_version: 1,
        critical: true,
        actor: host,
        data: { call_id: "call_1", decision: "allow", source: "policy" },
      },
      {
        type: "effect_begin",
        type_version: 1,
        critical: true,
        actor: host,
        data: { call_id: "call_1", attempt: 1 },
      },
      {
        type: "effect_unknown",
        type_version: 1,
        critical: true,
        actor: recovery,
        data: { call_id: "call_1", reason: "crash_after_begin" },
      },
      ...effects,
    ]),
  );
  h.clock.now += 10;
}

describe("a settled effect is never dispatched again (invariant 3)", () => {
  test("confirmed_success without its tool_result is materialized, not re-run", async () => {
    const h = harness([EMAIL], [userInput("mail bob")], [FINAL]);
    const out = new TextEncoder().encode("sent m-1");
    const sha256 = h.artifacts.put(out);
    crashedCall(h, [
      {
        type: "effect_resolved",
        type_version: 1,
        critical: true,
        actor: recovery,
        data: {
          call_id: "call_1",
          outcome: "confirmed_success",
          by: "reconcile",
          result_ref: { sha256, bytes: out.length, media_type: "text/plain" },
        },
      },
    ]);
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    const end = await resume(writer, h.artifacts, h.config());
    expect(end.kind).toBe("idle");
    expect(h.runs.get("send_email") ?? 0).toBe(0);
    const types = events(writer).map((e) => e.type);
    expect(types.filter((t) => t === "effect_begin")).toHaveLength(1);
    const result = events(writer).find((e) => e.type === "tool_result");
    expect(result?.data).toMatchObject({
      call_id: "call_1",
      is_error: false,
      preview: "sent m-1",
    });
  });

  test("interrupted without its tool_result closes the call, not re-run", async () => {
    const h = harness([EMAIL], [userInput("mail bob")], [FINAL]);
    crashedCall(h, [
      {
        type: "effect_resolved",
        type_version: 1,
        critical: true,
        actor: recovery,
        data: {
          call_id: "call_1",
          outcome: "interrupted",
          by: "sandbox_terminated",
        },
      },
    ]);
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    expect((await resume(writer, h.artifacts, h.config())).kind).toBe("idle");
    expect(h.runs.get("send_email") ?? 0).toBe(0);
    expect(
      events(writer).find((e) => e.type === "tool_result")?.data,
    ).toMatchObject({ origin: "interrupted", is_error: true });
  });

  test("not_sent re-dispatches under the same key with the next attempt", async () => {
    const h = harness([EMAIL], [userInput("mail bob")], [FINAL]);
    crashedCall(h, [
      {
        type: "effect_resolved",
        type_version: 1,
        critical: true,
        actor: recovery,
        data: { call_id: "call_1", outcome: "not_sent", by: "adapter" },
      },
    ]);
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    expect((await resume(writer, h.artifacts, h.config())).kind).toBe("idle");
    expect(h.runs.get("send_email")).toBe(1);
    const begins = events(writer).filter((e) => e.type === "effect_begin");
    expect<unknown>(begins.map((e) => e.data)).toEqual([
      { call_id: "call_1", attempt: 1 },
      { call_id: "call_1", attempt: 2 },
    ]);
  });

  // the provider's window runs from the first send; a deduplicated re-send doesn't
  // renew it. 10 s window, sent at t=0, re-sent at t=8, recovery at t=16: that parks.
  test("the dedup window is measured from the earliest begin, not the latest", async () => {
    const charge = {
      ...EMAIL,
      effect_class: "idempotent",
      dedup_window_ms: 10_000,
    } as const;
    const h = harness([charge], [userInput("charge")], []);
    const t0 = h.clock.now;
    crashedCall(h, [
      {
        type: "effect_resolved",
        type_version: 1,
        critical: true,
        actor: recovery,
        data: {
          call_id: "call_1",
          outcome: "safe_to_retry",
          by: "provider_dedup",
        },
      },
    ]);
    h.clock.now = t0 + 8000;
    const resend = unwrap(h.store.acquire(ROOT, "resender", 1));
    unwrap(
      resend.append([
        {
          type: "effect_begin",
          type_version: 1,
          critical: true,
          actor: host,
          data: { call_id: "call_1", attempt: 2 },
        },
      ]),
    );
    h.clock.now = t0 + 16_000;
    const writer = unwrap(h.store.acquire(ROOT, "owner"));
    expect((await resume(writer, h.artifacts, h.config())).kind).toBe("parked");
    expect(h.runs.get("send_email") ?? 0).toBe(0);
    expect(events(writer).at(-1)).toMatchObject({
      type: "parked",
      data: { reason: "effect_unknown" },
    });
  });
});
