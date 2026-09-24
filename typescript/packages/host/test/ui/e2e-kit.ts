import type { Message } from "@ag-ui/client";
import {
  type Agent,
  agent,
  type Model,
  type Store,
  scriptedModel,
  tool,
} from "@threads/core";
import { markTestKit } from "@threads/core/adapter";
import {
  type EventOf,
  knownEvents,
  openStore,
  type Principal,
  type ThreadId,
  tenantStore,
} from "@threads/core/host";
import { z } from "zod";
import type { Chunk } from "../../src/ui/frame";
import { say, use } from "../kit";
import type { Fetcher } from "./clients";
import { agUiFoldFrom, aiSdkFold } from "./stock";

// The end-to-end tests' kit: a scripted model that streams its text a word at a time and can
// hold a response open, the SSE frames of a host route, and each client's "uninterrupted" state
// of a run, folded by the stock client from the committed-frames route from the run's start.

/** A latch the test opens; `wait` resolves once it is open. */
export class Gate {
  readonly #open = Promise.withResolvers<void>();
  wait(): Promise<void> {
    return this.#open.promise;
  }
  open(): void {
    this.#open.resolve();
  }
}

/**
 * The scripted `responses`, with each text delta split into words. The response at `holdAt`
 * (0-based) streams its text, then waits for `gate` before it completes, so a test can drop a
 * connection or open another one while the text is live.
 */
export function liveModel(
  responses: readonly unknown[],
  hold?: { readonly at: number; readonly gate: Gate },
): Model {
  const inner = scriptedModel({ responses: [...responses] });
  let sent = 0;
  const made: Model = {
    ...inner,
    send: async function* (...args: Parameters<Model["send"]>) {
      const mine = sent;
      sent += 1;
      for await (const c of inner.send(...args)) {
        if (c.kind === "delta") {
          for (const word of c.text.split(/(?<= )/))
            yield { kind: "delta", part: c.part, text: word };
          continue;
        }
        if (c.kind === "done" && hold?.at === mine) await hold.gate.wait();
        yield c;
      }
    },
  };
  markTestKit(made);
  return made;
}

const DataLine = z.string().startsWith("data: ");
const Frame = z.custom<Chunk>(
  (v) => typeof v === "object" && v !== null && "type" in v,
);

/** One SSE message's data as a frame. */
export function chunkOf(data: string): Chunk {
  return Frame.parse(JSON.parse(data));
}

/** A route's SSE body as its data frames; `[DONE]` is left out. */
export async function frames(response: Response): Promise<readonly Chunk[]> {
  const text = await response.text();
  return text
    .split("\n\n")
    .flatMap((block) => block.split("\n"))
    .filter((l) => DataLine.safeParse(l).success)
    .map((l) => l.slice("data: ".length))
    .filter((d) => d !== "[DONE]")
    .map(chunkOf);
}

function get(h: Fetcher, as: Principal, path: string): Promise<Response> {
  return h.fetch(
    new Request(`http://host.test${path}`, {
      headers: { "x-principal": JSON.stringify(as) },
    }),
  );
}

/** The committed frames of a run from its start (the cursor route). */
export async function committed(
  h: Fetcher,
  as: Principal,
  thread: string,
  run: string,
  protocol: "ai-sdk" | "ag-ui",
): Promise<readonly Chunk[]> {
  return frames(
    await get(h, as, `/v1/threads/${thread}/runs/${run}/ui/${protocol}`),
  );
}

/** An uninterrupted AI SDK client's assistant message for the run. */
export async function aiSdkUninterrupted(
  h: Fetcher,
  as: Principal,
  thread: string,
  run: string,
): Promise<unknown> {
  return aiSdkFold(await committed(h, as, thread, run, "ai-sdk"));
}

/** An uninterrupted AG-UI client's messages: each run's user message, then its stream. */
export async function agUiUninterrupted(
  h: Fetcher,
  as: Principal,
  thread: string,
  runs: readonly { readonly run: string; readonly user: Message }[],
): Promise<readonly unknown[]> {
  let messages: readonly Message[] = [];
  for (const r of runs) {
    const stream = await committed(h, as, thread, r.run, "ag-ui");
    messages = await agUiFoldFrom([...messages, r.user], stream);
  }
  return messages;
}

const Timeline = z.object({
  entries: z.array(
    z.object({
      event: z.looseObject({
        type: z.string(),
        event_id: z.string(),
        data: z.looseObject({ client_message_id: z.string().optional() }),
      }),
    }),
  ),
});

/** The thread's runs, from its timeline route: each user_input's event id and message id. */
export async function recordedRuns(
  h: Fetcher,
  as: Principal,
  thread: string,
): Promise<
  readonly { readonly run: string; readonly messageId: string | undefined }[]
> {
  const r = await get(h, as, `/v1/threads/${thread}/timeline`);
  if (r.status === 404) return [];
  return Timeline.parse(await r.json())
    .entries.filter((e) => e.event.type === "user_input")
    .map((e) => ({
      run: e.event.event_id,
      messageId: e.event.data.client_message_id,
    }));
}

/** The user_input events on the thread's main branch. */
export async function inputs(
  store: Store,
  tenant: string,
  thread: ThreadId,
): Promise<readonly EventOf<"user_input">[]> {
  const { log } = await openStore(tenantStore(store, tenant));
  const main = log.mainBranch(thread);
  if (!main.ok) return [];
  const read = log.read(main.value);
  if (!read.ok) throw new Error(read.error.message);
  return knownEvents(read.value).filter(
    (e): e is EventOf<"user_input"> => e.type === "user_input",
  );
}

/**
 * An agent that calls a read-only lookup tool, then answers in text; `earlier` responses come
 * first (earlier turns). The text answer is held on `gate`.
 */
export function lookupAgent(
  gate: Gate,
  earlier: readonly unknown[] = [],
): Agent<undefined, string> {
  const lookup = tool({
    name: "lookup",
    description: "Look up an answer.",
    input: z.object({ q: z.string() }),
    runs: "host",
    effect: "read_only",
    execute: async () => "42",
  });
  const responses = [
    ...earlier,
    use("lookup", { q: "answer" }, "c1"),
    say("The answer is forty two."),
  ];
  return agent({
    name: "support",
    model: liveModel(responses, { at: earlier.length + 1, gate }),
    tools: [lookup],
  });
}
