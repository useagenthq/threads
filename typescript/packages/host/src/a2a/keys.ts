import { PROVENANCE } from "@threads/a2a/protocol";
import {
  canonicalize,
  type JsonObject,
  JsonValue,
  type Principal,
  principalKey,
  sha256Hex,
  type ThreadId,
} from "@threads/core/host";
import type { z } from "zod";
import { derivedId, derivedThreadId, derivedUuid } from "../derive";

// The receipt key, the context → thread derivation, and the ids the exposed side derives rather
// than generates. Nothing here is random: a frame, a task id and a status message must be the
// same on every read of the same committed log, in both languages. The receipt's operation literal
// lives with the other operations, in ../receipts.ts.

/** A messageId over this many UTF-8 bytes is refused: it would not fit the key readably. */
const MAX_MESSAGE_ID_BYTES = 255;

const utf8 = new TextEncoder();

/**
 * `"a2a/"` then, for each of principal_key, the agent and the messageId, its UTF-8 byte length,
 * `":"` and the value. Length-prefixed, so no two field splits collide, and the principal is in
 * the key, so two callers' equal messageIds never collide.
 */
export function sendKey(
  principal: Principal,
  agent: string,
  messageId: string,
): string {
  const fields = [principalKey(principal), agent, messageId];
  return `a2a/${fields.map((f) => `${utf8.encode(f).length}:${f}`).join("")}`;
}

/** Whether a messageId can be keyed at all; the refusal is InvalidParamsError at the route. */
export function messageIdTooLong(messageId: string): boolean {
  return utf8.encode(messageId).length > MAX_MESSAGE_ID_BYTES;
}

/**
 * The thread a context names. A separate domain string from the UI's chat key is required: an
 * A2A context must never be able to collide with a browser chat, and the derivation is one-way,
 * so a caller cannot reach another caller's thread by guessing a contextId either.
 */
export function a2aThreadId(
  principal: Principal,
  agent: string,
  contextId: string,
): ThreadId {
  return derivedThreadId("threads-a2a-v1", [
    principalKey(principal),
    agent,
    contextId,
  ]);
}

/**
 * sha256 of the canonical JSON of `{agent, message}`: what a reused messageId is checked against.
 *
 * The message hashed is the caller's **own JSON**, not the parsed model. Two hosts on one store may
 * be a TypeScript one and a Python one, and they must agree on this hash or the same messageId
 * would look like two different messages. Hashing the bytes the caller sent makes them agree by
 * construction, instead of by both dropping absent optional fields the same way.
 */
export function bodyHash(agent: string, message: unknown): string | undefined {
  const value = JsonValue.safeParse(message);
  if (!value.success) return undefined;
  const text = canonicalize({ agent, message: value.data });
  return text.ok ? sha256Hex(text.value) : undefined;
}

/** The caller's `message` as it arrived, for hashing. Absent or not JSON is undefined. */
export function rawMessage(params: unknown): unknown {
  return typeof params === "object" && params !== null && "message" in params
    ? Reflect.get(params, "message")
    : undefined;
}

/**
 * A status message's id, derived from the task and the seq it is read at, so the same committed
 * state always renders the same message. Both fields are fixed-width, so no length prefix.
 */
export function statusMessageId(taskId: string, seq: number): string {
  return derivedUuid("threads/a2a-status", [taskId, String(seq)]);
}

/** A completed task's one artifact id, derived from the task. */
export function artifactId(taskId: string): string {
  return derivedUuid("threads/a2a-artifact", [taskId]);
}

/**
 * The id of a task we refuse before it exists (a claimed hop count too deep). Derived from the
 * request, so a retry of the same message is answered the same way and nothing is stored.
 */
export function rejectedTaskId(
  principal: Principal,
  agent: string,
  messageId: string,
): string {
  return derivedId("threads-a2a-rejected-v1", [
    principalKey(principal),
    agent,
    messageId,
  ]);
}

/** The hop count at which a call chain is refused. A liar can only use it against itself. */
export const MAX_HOPS = 8;

/**
 * The caller's provenance claim, recorded as an untrusted claim and nothing else: it grants no
 * authority, picks no budget and never becomes the run's provenance.principal.
 */
export function claims(
  metadata: z.infer<typeof JsonObject> | undefined,
): z.infer<typeof JsonObject> | undefined {
  const found = metadata?.[PROVENANCE];
  return typeof found === "object" && found !== null && !Array.isArray(found)
    ? found
    : undefined;
}

/** A claimed hop count, when the claim carries a usable one. */
export function claimedHops(
  claim: z.infer<typeof JsonObject> | undefined,
): number | undefined {
  const hops = claim?.["hops"];
  return typeof hops === "number" && Number.isInteger(hops) && hops >= 0
    ? hops
    : undefined;
}
