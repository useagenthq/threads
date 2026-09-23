import { describe, expect, test } from "bun:test";
import type { KnownEvent } from "../../src/log";
import { resume } from "../../src/loop";
import { ROOT, unwrap } from "../store/helpers";
import { events } from "./harness";
import {
  abandon,
  asked,
  crashed,
  guide,
  outcome,
  reply,
  resumeOnce,
  SUMMARY_TEXT,
  side,
  sides,
} from "./manual-kit";

// A crash at every boundary of a requested compaction: the resumed run reads the stage from the
// log and makes the same decision each time, never sending again what the log already settled.

const lastLine = (h: ReturnType<typeof asked>, log: readonly KnownEvent[]) => {
  const request = sides(log).at(-1);
  if (request?.type !== "model_request") throw new Error("a side request");
  const bytes = unwrap(h.artifacts.get(request.data.request_ref.sha256));
  return new TextDecoder().decode(bytes).trimEnd().split("\n").at(-1) ?? "";
};

/** `count` side attempts, each abandoned by recovery as a crash. */
function crashes(h: ReturnType<typeof asked>, count: number): void {
  for (let n = 1; n <= count; n++) {
    crashed(h, (log) => [side(h, log, n)]);
    crashed(h, (log) => [abandon(log, "crash")]);
  }
}

describe("resuming a requested compaction", () => {
  test("after the first of two guides: the first hook isn't called again, both guides once", async () => {
    const calls = new Map<string, number>();
    const h = asked([reply(SUMMARY_TEXT), reply("A.")]);
    crashed(h, () => [
      {
        type: "hook_decision",
        type_version: 1,
        critical: true,
        actor: { kind: "host" },
        data: {
          extension: "alpha",
          hook: "before_compact",
          decision: "guide",
          reason: "guide from alpha",
        },
      },
    ]);
    const log = await resumeOnce(h, {
      extensions: [guide("alpha", calls), guide("beta", calls)],
    });
    expect(calls).toEqual(new Map([["beta", 1]]));
    const line = lastLine(h, log);
    expect(line.match(/guide from alpha/g)).toHaveLength(1);
    expect(line.match(/guide from beta/g)).toHaveLength(1);
    expect(outcome(log)).toMatchObject({ type: "compacted" });
  });

  test("after the side request was sent: recovery abandons it, then one resend", async () => {
    const h = asked([reply(SUMMARY_TEXT), reply("A.")]);
    crashed(h, (log) => [side(h, log, 1)]);
    const log = await resumeOnce(h);
    expect(log[0]).toMatchObject({
      type: "model_attempt_abandoned",
      data: { reason: "crash" },
    });
    expect(
      sides(log).map((e) => e.type === "model_request" && e.data.attempt),
    ).toEqual([2]);
    expect(outcome(log)).toMatchObject({ type: "compacted" });
  });

  for (const count of [1, 2]) {
    test(`${count} recovery-written crash abandonment(s): one resend, and resuming again sends nothing`, async () => {
      const h = asked([reply(SUMMARY_TEXT), reply("A.")]);
      crashes(h, count);
      const first = await resumeOnce(h);
      expect(sides(first)).toHaveLength(1);
      expect(outcome(first)).toMatchObject({ type: "compacted" });
      const second = await resumeOnce(h);
      expect(sides(second)).toHaveLength(0);
      expect(outcome(second)).toBeUndefined();
    });
  }

  test("3 crash abandonments exceed crash_resends: model_error with no side request, twice the same", async () => {
    const h = asked([reply("A.")]);
    crashes(h, 3);
    const first = await resumeOnce(h);
    expect(sides(first)).toHaveLength(0);
    expect(outcome(first)).toMatchObject({
      type: "compaction_failed",
      actor: { kind: "recovery" },
      data: { reason: "model_error" },
    });
    // The side request's crashes don't spend the turn's own crash budget: the turn answers.
    expect(first.at(-1)).toMatchObject({
      type: "turn_completed",
      data: { reason: "end_turn" },
    });
    const second = await resumeOnce(h);
    expect(second.filter((e) => e.type === "model_request")).toHaveLength(0);
    expect(outcome(second)).toBeUndefined();
  });

  for (const reason of ["provider_error", "rate_limited"] as const) {
    test(`a recorded ${reason} is answered from the log: model_error, no new side request`, async () => {
      const h = asked([reply("A.")]);
      crashed(h, (log) => [side(h, log, 1)]);
      crashed(h, (log) => [abandon(log, reason)]);
      const log = await resumeOnce(h);
      expect(sides(log)).toHaveLength(0);
      expect(outcome(log)).toMatchObject({ data: { reason: "model_error" } });
    });
  }

  test("prompt_too_long before the fallback: the fallback attempt runs once", async () => {
    const h = asked([reply(SUMMARY_TEXT), reply("A.")]);
    crashed(h, (log) => [side(h, log, 1)]);
    crashed(h, (log) => [abandon(log, "prompt_too_long")]);
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(1);
    expect(outcome(log)).toMatchObject({ type: "compacted" });
  });

  test("prompt_too_long after the fallback: the outcome, and no request", async () => {
    const h = asked([reply("A.")]);
    for (let n = 1; n <= 2; n++) {
      crashed(h, (log) => [side(h, log, n)]);
      crashed(h, (log) => [abandon(log, "prompt_too_long")]);
    }
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(0);
    expect(outcome(log)).toMatchObject({ data: { reason: "prompt_too_long" } });
  });

  test("a crash on attempt 1, then prompt_too_long on attempt 2: the fallback still runs", async () => {
    const h = asked([reply(SUMMARY_TEXT), reply("A.")]);
    crashed(h, (log) => [side(h, log, 1)]);
    crashed(h, (log) => [abandon(log, "crash")]);
    crashed(h, (log) => [side(h, log, 2)]);
    crashed(h, (log) => [abandon(log, "prompt_too_long")]);
    const log = await resumeOnce(h);
    expect(
      sides(log).map((e) => e.type === "model_request" && e.data.attempt),
    ).toEqual([3]);
    expect(outcome(log)).toMatchObject({ type: "compacted" });
  });

  test("after the summary was recorded: compacted from it, with no side request", async () => {
    const h = asked([reply("A.")]);
    crashed(h, (log) => [side(h, log, 1)]);
    crashed(h, (log) => {
      const request = sides(log).at(-1);
      if (request === undefined) throw new Error("a side request");
      return [
        {
          type: "model_response",
          type_version: 1,
          critical: true,
          actor: { kind: "model" },
          data: {
            request_event_id: request.event_id,
            content: [{ type: "text", text: SUMMARY_TEXT }],
            stop_reason: "end_turn",
            usage: { input_tokens: 10, output_tokens: 2 },
            completeness: "complete",
          },
        },
      ];
    });
    const log = await resumeOnce(h);
    expect(sides(log)).toHaveLength(0);
    expect(outcome(log)).toMatchObject({
      type: "compacted",
      actor: { kind: "recovery" },
    });
    expect(log.at(-1)).toMatchObject({ type: "turn_completed" });
  });

  test("the restore is in the batch of compacted: a crash right after it keeps both", async () => {
    const style = {
      type: "injected",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: {
        source: "output_style",
        trust: "trusted_instruction",
        origin: { id: "concise" },
        text: "Be brief.",
      },
    } as const;
    const h = asked(
      [reply(SUMMARY_TEXT)],
      { output_styles: { concise: "Be brief." } },
      [style],
    );
    const writer = unwrap(h.store.acquire(ROOT, "crashing"));
    await resume(
      writer,
      h.artifacts,
      h.config({
        onEvent: (e) => {
          if (e.type === "compacted") throw new Error("crash");
        },
      }),
    ).catch(() => undefined);
    const log = events(writer);
    const at = log.findIndex((e) => e.type === "compacted");
    expect(log[at + 1]).toMatchObject({
      type: "injected",
      data: { source: "output_style", text: "Be brief." },
    });
  });
});
