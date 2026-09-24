import type {
  BaseEvent,
  HttpAgent,
  Message,
  RunAgentParameters,
} from "@ag-ui/client";
import type { Principal } from "@threads/core/host";
import { z } from "zod";
import { uiThreadId } from "../../src/ui/key";
import type { Fetcher } from "./clients";
import { agUiUninterrupted, recordedRuns } from "./e2e-kit";
import { agUiProjection } from "./stock";

// The AG-UI end-to-end tests' kit: a user message, one runAgent that reports a failure instead
// of throwing (a dropped connection rejects), and "equals" as the lane defines it: the whole
// agent.messages against an uninterrupted client's, each run folded from its committed frames.

export function user(id: string, text: string): Message {
  return { id, role: "user", content: text };
}

export type Attempt = {
  /** Undefined when runAgent resolved; else its rejection (a drop, or the verifier). */
  readonly error: string | undefined;
  /** The events the client applied, each past its own verifier. */
  readonly events: readonly BaseEvent[];
};

/** runAgent, with a rejection as a value and the events it applied. */
export async function attempt(
  a: HttpAgent,
  parameters?: RunAgentParameters,
): Promise<Attempt> {
  const events: BaseEvent[] = [];
  const quiet = console.error;
  console.error = () => {};
  try {
    await a.runAgent(parameters, {
      onEvent: ({ event }) => void events.push(event),
    });
    return { error: undefined, events };
  } catch (e) {
    return { error: String(e), events };
  } finally {
    console.error = quiet;
  }
}

/** runAgent that must resolve. */
export async function succeed(
  a: HttpAgent,
  parameters?: RunAgentParameters,
): Promise<readonly BaseEvent[]> {
  const done = await attempt(a, parameters);
  if (done.error !== undefined) throw new Error(done.error);
  return done.events;
}

const Interrupt = z.object({
  id: z.string(),
  expiresAt: z.string().optional(),
});

/** The ids of the client's pending interrupts. */
export function pending(a: HttpAgent): string[] {
  return a.pendingInterrupts.map((i) => Interrupt.parse(i).id);
}

/**
 * The client's messages equal an uninterrupted client's: every run of the chat, each after its
 * own user message, from the runs the thread recorded.
 */
export async function uninterrupted(
  h: Fetcher,
  as: Principal,
  chat: string,
  users: readonly Message[],
): Promise<unknown[]> {
  const thread = uiThreadId(as, "support", chat);
  const runs = (await recordedRuns(h, as, thread)).map(({ run, messageId }) => {
    const u = users.find((m) => m.id === messageId);
    if (u === undefined) throw new Error(`no user message for run ${run}`);
    return { run, user: u };
  });
  return agUiProjection(await agUiUninterrupted(h, as, thread, runs));
}
