import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import {
  type BaseEvent,
  defaultApplyEvents,
  HttpAgent,
  type Message,
} from "@ag-ui/client";
import { from, lastValueFrom, toArray } from "rxjs";
import { z } from "zod";

// bun scripts/record-ag-ui-fold.ts          write spec/conformance/vectors/ag-ui-fold.json
// bun scripts/record-ag-ui-fold.ts --check  fail if the committed vectors differ
//
// Dev only. The messages @ag-ui/client's own defaultApplyEvents builds from AG-UI streams threads
// sends (taken from the `ui` cases), from an empty client and over a partial client state, so the
// Python port of foldAgUi is checked against the stock client, never against a hand-written
// answer. Recorded with the pinned @ag-ui/client; a version bump re-records in its own change.

const ROOT = join(import.meta.dir, "../../../../spec/conformance");
export const FOLD_VECTORS: string = join(ROOT, "vectors/ag-ui-fold.json");

const FROM_CASES = [
  "ui-text-and-tool-ag-ui",
  "ui-snapshot-fold-ag-ui",
  "ui-reasoning-summary-ag-ui",
  "ui-deferred-then-late-ag-ui",
  "ui-live-prefix-ag-ui",
  "ui-resume-settled-ag-ui",
] as const;

const Line = z.object({
  data: z.custom<BaseEvent>((v) => typeof v === "object"),
});
const Messages = z.array(z.custom<Message>((v) => typeof v === "object"));

function events(name: string): BaseEvent[] {
  return readFileSync(join(ROOT, "cases", name, "frames.jsonl"), "utf8")
    .split("\n")
    .filter((l) => l !== "")
    .map((l) => Line.parse(JSON.parse(l)).data);
}

async function applied(
  initial: readonly Message[],
  stream: readonly BaseEvent[],
): Promise<readonly Message[]> {
  const agent = new HttpAgent({
    url: "http://host.test",
    initialMessages: [...initial],
  });
  const input = {
    threadId: "t",
    runId: "r",
    messages: [],
    tools: [],
    context: [],
    state: {},
    forwardedProps: {},
  };
  const mutations = await lastValueFrom(
    defaultApplyEvents(input, from(stream), agent, []).pipe(toArray()),
    { defaultValue: [] },
  );
  return (
    mutations.findLast((m) => m.messages !== undefined)?.messages ?? initial
  );
}

/** A client that holds part of the run, plus a message of its own, gets the replay's snapshot. */
function partial(stream: readonly BaseEvent[]): readonly Message[] {
  const snapshot = z
    .object({
      messages: z.array(z.object({ id: z.string(), role: z.string() })),
    })
    .parse(stream.find((e) => e.type === "MESSAGES_SNAPSHOT"));
  const text = snapshot.messages.find((m) => m.id.endsWith(":0"));
  if (text === undefined) throw new Error("the replay holds a text message");
  return Messages.parse([
    { id: "local-1", role: "user", content: "a draft only this page has" },
    { id: text.id, role: "assistant", content: "Chec" },
  ]);
}

export async function vectors(): Promise<string> {
  const out: unknown[] = [];
  for (const name of FROM_CASES) {
    const stream = events(name);
    out.push({
      name,
      initial: [],
      events: stream,
      messages: await applied([], stream),
    });
  }
  const replay = events("ui-snapshot-fold-ag-ui");
  const initial = partial(replay);
  out.push({
    name: "a snapshot merged over a partial client state",
    initial,
    events: replay,
    messages: await applied(initial, replay),
  });
  return `${JSON.stringify(out, null, 2)}\n`;
}

if (import.meta.main) {
  const text = await vectors();
  if (process.argv[2] === "--check") {
    if (readFileSync(FOLD_VECTORS, "utf8") !== text) {
      console.error(`${FOLD_VECTORS} differs from the @ag-ui/client recording`);
      process.exitCode = 1;
    }
  } else {
    writeFileSync(FOLD_VECTORS, text);
    console.log(`wrote ${FOLD_VECTORS}`);
  }
}
