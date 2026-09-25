import { describe, expect, test } from "bun:test";
import type { KnownEvent } from "../../src/log";
import { Session } from "../../src/loop/session";
import { BARRED } from "../../src/loop/types";
import { err, ok } from "../../src/result";
import type { EventDraft } from "../../src/store";
import { ROOT, unwrap } from "../store/helpers";
import { harness, userInput } from "./harness";

// Session.appendDecided: a decided batch passes the cancel barrier like every loop append, and a
// refusal comes back as itself with nothing appended.

const CANCEL: EventDraft = {
  type: "cancel_requested",
  type_version: 1,
  critical: true,
  actor: {
    kind: "user",
    principal: { issuer: "api", tenant: "acme", subject: "alice" },
  },
  data: { scope: "turn" },
};

async function session(seen: KnownEvent[] = []): Promise<Session> {
  const h = await harness([], [userInput("go")], []);
  const writer = unwrap(await h.store.acquire(ROOT, "run"));
  return new Session(
    writer,
    h.artifacts,
    h.config({ onEvent: (e) => seen.push(e) }),
  );
}

describe("Session.appendDecided", () => {
  test("a decided batch is appended and its events reach onEvent", async () => {
    const seen: KnownEvent[] = [];
    const s = await session(seen);
    expect(await s.appendDecided(async () => ok([CANCEL]))).toBeUndefined();
    expect(seen.map((e) => e.type)).toEqual(["cancel_requested"]);
  });

  test("a refusal comes back and nothing is appended", async () => {
    const s = await session();
    const before = s.fold.seq;
    expect(await s.appendDecided(async () => err("mailbox_full"))).toEqual({
      kind: "refused",
      refusal: "mailbox_full",
    });
    expect(s.fold.seq).toBe(before);
  });

  test("after a cancel the barrier keeps no other turn ending", async () => {
    const s = await session();
    expect(await s.append(CANCEL)).toBeUndefined();
    const before = s.fold.seq;
    const ended: EventDraft = {
      type: "turn_completed",
      type_version: 1,
      critical: true,
      actor: { kind: "host" },
      data: { reason: "end_turn" },
    };
    expect(await s.appendDecided(async () => ok([ended]))).toBe(BARRED);
    expect(s.fold.seq).toBe(before);
  });
});
