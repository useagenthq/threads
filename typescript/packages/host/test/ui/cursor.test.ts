import { afterEach, describe, expect, test } from "bun:test";
import type { Message } from "@ag-ui/client";
import { uiThreadId } from "../../src/ui/key";
import { alice, type Harness, harness } from "../kit";
import { chunkOf, Gate, inputs, lookupAgent } from "./e2e-kit";
import { agUiFoldFrom, agUiProblems, agUiProjection } from "./stock";

// The committed-frames route resumed at every frame boundary (spec/schema/ui/README.md,
// "Cursors"): AI SDK continues with exactly the frames after the cursor; AG-UI starts a new run
// whose snapshot folds to the same messages as the whole stream.

let h: Harness | undefined;
afterEach(async () => {
  await h?.host.stop();
  h = undefined;
});

type Block = { readonly id: string | undefined; readonly data: string };

async function blocks(r: Response): Promise<readonly Block[]> {
  expect(r.status).toBe(200);
  return (await r.text())
    .split("\n\n")
    .filter((b) => b !== "")
    .map((b) => {
      const lines = b.split("\n");
      const id = lines.find((l) => l.startsWith("id: "))?.slice(4);
      const data = lines.find((l) => l.startsWith("data: "))?.slice(6) ?? "";
      return { id, data };
    });
}

async function ran(): Promise<{ readonly on: Harness; readonly path: string }> {
  const gate = new Gate();
  gate.open();
  const on = harness({ agents: { support: lookupAgent(gate) } });
  h = on;
  const posted = await on.call("POST", "/v1/ui/ai-sdk/support", {
    as: alice,
    body: {
      id: "chat-1",
      messages: [
        {
          id: "m1",
          role: "user",
          parts: [{ type: "text", text: "What is it?" }],
        },
      ],
      trigger: "submit-message",
    },
  });
  await posted.text();
  const thread = uiThreadId(alice, "support", "chat-1");
  const [run] = await inputs(on.store, "acme", thread);
  if (run === undefined) throw new Error("no run");
  return { on, path: `/v1/threads/${thread}/runs/${run.event_id}/ui` };
}

describe("the cursor route", () => {
  test("AI SDK: after each frame, the opening, then exactly the frames that follow it", async () => {
    const { on, path } = await ran();
    const whole = await blocks(
      await on.call("GET", `${path}/ai-sdk`, { as: alice }),
    );
    const [opening] = whole;
    if (opening === undefined) throw new Error("an empty stream");
    expect(whole.at(-1)?.data).toBe("[DONE]");
    const logged = (bs: readonly Block[]) =>
      bs.filter((b) => b.id !== undefined);
    const ids = logged(whole).map((b) => b.id);
    expect(ids.length).toBeGreaterThan(5);
    const closing = whole.slice(
      whole.findLastIndex((b) => b.id !== undefined) + 1,
    );
    const resumedAfter = (i: number) => [
      opening,
      ...logged(whole).slice(i + 1),
      ...closing,
    ];
    for (const [i, id] of ids.entries()) {
      const rest = await blocks(
        await on.call("GET", `${path}/ai-sdk?after=${id}`, { as: alice }),
      );
      expect(rest).toEqual(resumedAfter(i));
    }
    const header = await on.call("GET", `${path}/ai-sdk?after=999999:0`, {
      as: alice,
      headers: { "last-event-id": ids[0] ?? "" },
    });
    expect(await blocks(header)).toEqual(resumedAfter(0));
  });

  test("AG-UI: after each frame, a new run whose snapshot folds to the whole stream's messages", async () => {
    const { on, path } = await ran();
    const whole = await blocks(
      await on.call("GET", `${path}/ag-ui`, { as: alice }),
    );
    const asked: Message = { id: "m1", role: "user", content: "What is it?" };
    const chunks = (bs: readonly Block[]) => bs.map((b) => chunkOf(b.data));
    const want = agUiProjection(await agUiFoldFrom([asked], chunks(whole)));
    for (const b of whole) {
      if (b.id === undefined) continue;
      const resumed = chunks(
        await blocks(
          await on.call("GET", `${path}/ag-ui?after=${b.id}`, { as: alice }),
        ),
      );
      expect(resumed[0]?.type).toBe("RUN_STARTED");
      expect(agUiProblems(resumed)).toEqual([]);
      expect(agUiProjection(await agUiFoldFrom([asked], resumed))).toEqual(
        want,
      );
    }
  });
});
