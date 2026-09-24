import { expect, test } from "bun:test";
import { agent, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { hostRunner } from "../../src/host";
import { createIssue, lookup } from "./tool-search-kit";

// The host's pin of a new thread (spec/schema/README.md, "Deferred tools and tool_search"): a pin
// that is only compared writes nothing; put() stores the spec artifacts before an append.

test("started() writes no artifact; put() stores the spec artifacts its event names", async () => {
  const model = scriptedModel({ responses: [] });
  const runner = hostRunner(agent({ model, tools: [lookup, createIssue] }));
  if (runner === undefined) throw new Error("agent() registers a host runner");
  const store = sqlite(":memory:");
  const { artifacts } = await openStore(store);
  const pin = await runner.started();
  if (pin.event.type !== "thread_started") throw new Error("a thread_started");
  const ref = pin.event.data.tools.find(
    (t) => t.name === "create_issue",
  )?.spec_ref;
  if (ref === undefined) throw new Error("create_issue is pinned by reference");
  expect(artifacts.get(ref.sha256).ok).toBe(false);
  await pin.put(store);
  expect(artifacts.get(ref.sha256).ok).toBe(true);
});
