import type { JsonObject } from "threadsai/adapter";
import type { z } from "zod";
import { outbound, type Wire } from "../protocol";
import type { Claim } from "./derive";

// The one request body a call ever has. It is built once, stored once, and re-sent unchanged: a
// re-serialization that differed by one byte would defeat a peer's deduplication while looking
// correct, so the bytes are the record and this function runs only on a call's first attempt.

export type Building = {
  readonly wire: Wire;
  readonly messageId: string;
  readonly contextId: string;
  readonly taskId: string | undefined;
  readonly text: string;
  readonly metadata: Record<string, Claim> | undefined;
};

export type Built = {
  readonly bytes: string;
  readonly messageId: string;
  readonly contextId: string;
};

export function requestBody(b: Building): Built {
  const message = {
    messageId: b.messageId,
    contextId: b.contextId,
    ...(b.taskId === undefined ? {} : { taskId: b.taskId }),
    role: "ROLE_USER",
    parts: [{ text: b.text }],
    ...(b.metadata === undefined ? {} : { metadata: b.metadata }),
  };
  const params = {
    message,
    // The first response is the only one whose loss matters, so it is small and fast.
    configuration: { returnImmediately: true },
  };
  // The envelope id is the derived messageId: unique per call, and derived rather than random so
  // the bytes this module stores are the same on every build of the same call.
  const built = outbound(b.wire, "SendMessage", params, b.messageId);
  if (built.body === undefined)
    throw new Error("SendMessage carries its request in the body");
  return { bytes: built.body, messageId: b.messageId, contextId: b.contextId };
}

/** `ListTasks` for one context: how reconciliation asks a partner what it already has. */
export function listParams(
  contextId: string,
  pageSize: number,
): z.infer<typeof JsonObject> {
  // One page only: a partner that truncates history never proves absence anyway, so a second page
  // would add latency to an answer that still cannot settle a park.
  return { contextId, pageSize, historyLength: HISTORY };
}

/** Enough history for our own message to be visible when the partner keeps any at all. */
const HISTORY = 100;
