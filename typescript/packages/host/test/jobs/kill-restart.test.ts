import { afterEach, describe, expect, test } from "bun:test";
import {
  events,
  expireLeases,
  finish,
  kill,
  log,
  logged,
  reap,
  scratch,
  sends,
  spawn,
  waitAt,
} from "./drill";
import { rows } from "./worker";

// Job kill-restart-acked-events: a host process is killed with
// SIGKILL at each durable boundary of a channel run, then restarted on the same store (twice,
// the provider redelivering its webhook each time). Every acked event is still there, the run
// goes on from the log, nothing is sent twice, and a send whose outcome can't be proven parks.

afterEach(reap);

/** [stop point, what the fake channel's lookup can prove] */
const POINTS = [
  ["webhook_ack", "final"],
  ["user_input", "final"],
  ["model_request", "final"],
  ["effect_begin", "final"],
  ["effect_begin", "none"],
  ["sent", "final"],
  ["sent", "none"],
  ["sent", "unknown"],
  ["effect_commit", "final"],
] as const;

const replyOf = async (where: string): Promise<string | undefined> =>
  [...((await log(where))?.fold.calls.keys() ?? [])].find((id) =>
    id.startsWith("send_"),
  );

describe("kill-restart-acked-events", () => {
  test.each(POINTS)(
    "killed at %s (lookup %s): restarts from the log",
    async (point, lookup) => {
      const dir = scratch();
      const env = { DRILL_WEBHOOK: "1", DRILL_LOOKUP: lookup };
      const first = spawn("serve", dir, { ...env, DRILL_STOP_AT: point });
      await waitAt(first, point);
      await kill(first);
      await expireLeases(dir);
      const acked = (await events(dir)).map((e) => e.event_id);
      const sentBefore = sends(dir);

      expect(await finish(spawn("serve", dir, env))).toBe(0);
      const after = await events(dir);
      expect(after.map((e) => e.event_id).slice(0, acked.length)).toEqual(
        acked,
      );
      const count = (type: string) =>
        after.filter((e) => e.type === type).length;
      expect(count("channel_delivery")).toBe(1);
      expect(count("user_input")).toBe(1);
      expect(new Set(rows(dir, "acks.jsonl").map((r) => r["status"]))).toEqual(
        new Set([200]),
      );
      const keys = sends(dir);
      expect(new Set(keys).size).toBe(keys.length);

      const fold = (await log(dir))?.fold;
      const reply = (await replyOf(dir)) ?? "";
      if (
        lookup !== "final" &&
        (point === "effect_begin" || point === "sent")
      ) {
        // Unproven: parked, never re-sent, never reported acknowledged.
        expect(fold?.parked.map((p) => p.kind)).toEqual(["effect"]);
        expect(keys).toEqual(sentBefore);
        expect(fold?.calls.get(reply)?.result).toBeUndefined();
        expect(
          after.slice(acked.length).some((e) => e.type === "effect_commit"),
        ).toBe(false);
      } else {
        expect(fold?.parked).toEqual([]);
        expect(keys.map((k) => k.split(":")[1])).toEqual([reply]);
        expect(rows(dir, "sends.jsonl").map((r) => r["text"])).toEqual([
          "done",
        ]);
      }

      // A second restart and redelivery finds nothing to do: nothing appended, nothing sent.
      const settled = after.map((e) => e.event_id);
      expect(await finish(spawn("serve", dir, env))).toBe(0);
      expect((await events(dir)).map((e) => e.event_id)).toEqual(settled);
      expect(sends(dir)).toEqual(keys);
      expect(logged(dir)).toBe("");
    },
    60_000,
  );
});
