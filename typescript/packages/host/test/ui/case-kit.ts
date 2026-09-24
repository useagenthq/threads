import { sqlite } from "@threads/core";
import {
  canonicalize,
  EventId,
  type KnownEvent,
  knownEvents,
  openStore,
  type ParkAddress,
  ThreadId,
  tenantStore,
  threadHandle,
} from "@threads/core/host";
import { z } from "zod";
import type { Case } from "../../../core/test/conformance/cases";
import { outcomeFromLog, type RunOutcome } from "../../src/outcome";
import { resumeConflicts } from "../../src/ui/ag-ui-resume";
import type { Frame } from "../../src/ui/frame";
import { LiveHub } from "../../src/ui/hub";
import { LiveListener } from "../../src/ui/listener";
import { type SessionPlan, UiSession } from "../../src/ui/session";

// The `ui` conformance runner's driver (spec/conformance/README.md, "ui"): the host's own
// session, hub and listener, moved by the case's timeline instead of a route, over the log the
// case imports.

const Step = z.union([
  z.strictObject({ register: z.literal(true) }),
  z.strictObject({
    delta: z.strictObject({
      request_event_id: z.string(),
      part: z.int(),
      text: z.string(),
    }),
  }),
  z.strictObject({ commit: z.int() }),
]);
type Step = z.infer<typeof Step>;

const ResumeEntry = z.strictObject({
  interruptId: z.string(),
  status: z.enum(["resolved", "cancelled"]),
  payload: z.json().optional(),
});

const UiInput = z.strictObject({
  protocol: z.enum(["ai-sdk", "ag-ui"]),
  run_id: EventId,
  ids: z.strictObject({ threadId: z.string(), runId: z.string() }).optional(),
  after: z.string().optional(),
  replay: z.literal(true).optional(),
  resume: z.array(ResumeEntry).optional(),
  receipts: z.record(z.string(), z.string()).optional(),
  live: z.array(Step).optional(),
});
type UiInput = z.infer<typeof UiInput>;

type Log = {
  readonly events: readonly KnownEvent[];
  readonly outcome: (visible: readonly KnownEvent[]) => RunOutcome | undefined;
};

/** The case's log, imported into a fresh store, and the run's outcome at any head. */
async function imported(c: Case, runId: EventId): Promise<Log> {
  const store = tenantStore(sqlite(":memory:"), "acme");
  const opened = await openStore(store);
  for (const a of c.artifacts) await opened.artifacts.put(a);
  if (c.log === undefined) throw new Error("a ui case has a log");
  const log = opened.log.importLog(c.log);
  if (!log.ok) throw new Error(`${log.error.code}: ${log.error.message}`);
  const events = knownEvents(log.value);
  const first = events[0];
  if (first === undefined) throw new Error("an empty log");
  const thread = threadHandle(opened, {
    id: ThreadId.parse(first.thread_id),
    branch: first.branch_id,
    store,
  });
  return {
    events,
    outcome: (visible) =>
      outcomeFromLog(visible, runId, parks(visible), thread),
  };
}

function parks(events: readonly KnownEvent[]): readonly ParkAddress[] {
  const open: ParkAddress[] = [];
  for (const e of events)
    if (e.type === "parked") open.push(e.data.address);
    else if (e.type === "resumed") {
      const at = open.findIndex(
        (a) => a.kind === e.data.address.kind && a.id === e.data.address.id,
      );
      if (at !== -1) open.splice(at, 1);
    }
  return open;
}

function plan(log: Log, input: UiInput, now: number): SessionPlan {
  const first = log.events[0];
  const extra = resumeConflicts(
    { events: log.events, parked: parks(log.events) },
    input.resume ?? [],
    now,
  );
  if (!extra.ok) throw new Error(extra.error.message);
  const cursor = input.after?.split(":").map(Number);
  const after =
    cursor === undefined
      ? {}
      : input.protocol === "ag-ui"
        ? { replay: cursor[0] ?? 0 }
        : { after: { seq: cursor[0] ?? 0, k: cursor[1] ?? 0 } };
  return {
    protocol: input.protocol,
    runId: input.run_id,
    ids: input.ids ?? { threadId: first?.thread_id ?? "", runId: input.run_id },
    ...after,
    ...(input.replay === true ? { replay: "head" } : {}),
    extra: extra.value,
    receipts: new Map(Object.entries(input.receipts ?? {})),
  };
}

type Served = {
  readonly frames: readonly Frame[];
  /** The stream ended cleanly (the AI SDK then sends [DONE]); false: closed on a broken part. */
  readonly done: boolean;
};

export type UiRun = Served & {
  readonly protocol: "ai-sdk" | "ag-ui";
  /**
   * For an AI SDK cursor: the canonical stream through it, which the client already has. A
   * custom client continues it frame by frame, so the runner folds the two as one stream.
   */
  readonly received: readonly Frame[];
};

/** One `ui` case served as the README's runner steps say. */
export async function runUiCase(c: Case): Promise<UiRun> {
  const input = UiInput.parse(c.input);
  const log = await imported(c, input.run_id);
  const served = serve(log, input, c.now);
  if (input.protocol !== "ai-sdk" || input.after === undefined)
    return { ...served, protocol: input.protocol, received: [] };
  const { after, ...whole } = input;
  const full = serve(log, whole, c.now).frames;
  const upto = full.findIndex((f) => f.id === after);
  return {
    ...served,
    protocol: input.protocol,
    received: full.slice(0, upto + 1),
  };
}

/** One connection over the case's timeline. */
function serve(log: Log, input: UiInput, now: number): Served {
  const last = log.events.at(-1)?.seq ?? 0;
  const steps: readonly Step[] = input.live ?? [
    { register: true },
    { commit: last },
  ];
  const timeline = new Timeline(log, input.live !== undefined);
  const out: Frame[] = [];
  let session: UiSession | undefined;
  for (const step of steps) {
    const listener = timeline.apply(step);
    if (listener === undefined || "register" in step) continue;
    const events = timeline.visible();
    session ??= opened(plan(log, input, now), events, listener, out);
    out.push(...session.deltas(listener.take()));
    const read = session.read(events);
    out.push(...read.frames);
    if (read.broken !== undefined) return { frames: out, done: false };
    const outcome = log.outcome(events);
    if (outcome !== undefined) {
      out.push(...session.close(outcome));
      return { frames: out, done: true };
    }
  }
  throw new Error("a ui case's timeline ends with the run's outcome");
}

/** The hub and the committed head, moved by the case's steps. */
class Timeline {
  readonly #log: Log;
  readonly #thread: string;
  readonly #hub = new LiveHub();
  readonly #live: boolean;
  #head = 0;
  #listener: LiveListener | undefined;

  constructor(log: Log, live: boolean) {
    this.#log = log;
    this.#thread = log.events[0]?.thread_id ?? "";
    this.#live = live;
  }

  /** Applies a step; the listener once one is registered. */
  apply(step: Step): LiveListener | undefined {
    if ("register" in step)
      this.#listener = new LiveListener(
        this.#live ? this.#hub : undefined,
        "acme",
        this.#thread,
      );
    else if ("delta" in step) {
      const d = step.delta;
      this.#hub.delta("acme", this.#thread, {
        requestId: d.request_event_id,
        part: d.part,
        text: d.text,
      });
    } else {
      for (const e of this.#log.events)
        if (e.seq > this.#head && e.seq <= step.commit)
          this.#hub.appended("acme", e);
      this.#head = step.commit;
    }
    return this.#listener;
  }

  visible(): readonly KnownEvent[] {
    return this.#log.events.filter((e) => e.seq <= this.#head);
  }
}

function opened(
  p: SessionPlan,
  events: readonly KnownEvent[],
  listener: LiveListener,
  out: Frame[],
): UiSession {
  const session = new UiSession(p, events, listener.missed);
  out.push(...session.opening(events));
  return session;
}

export function line(f: Frame | "[DONE]"): string {
  const text = canonicalize(f === "[DONE]" ? { data: f } : f);
  if (!text.ok) throw new Error(text.error.message);
  return text.value;
}
