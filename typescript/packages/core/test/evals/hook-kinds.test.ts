import { expect, test } from "bun:test";
import {
  type AgentOptions,
  agent,
  type Extension,
  extension,
  type Hooks,
  scriptedModel,
  sqlite,
} from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { HOOK_KINDS, type HookKind } from "../../src/evals/kinds";
import type { HookName } from "../../src/hooks/types";
import { HookDecisionData, type KnownEvent } from "../../src/log";
import { knownEvents } from "../../src/reduce";
import { asked, reply, resumeOnce, SUMMARY_TEXT } from "../loop/manual-kit";
import { lookupOrder, say, use } from "./kit";

// The hook-kinds probe (spec lane 22, A.2): the real loop, once per hook, with a no-op extension
// that defines only that hook. A run that appends a hook_decision makes the hook "recorded"; one
// that appends none makes it "observation". The result must equal HOOK_KINDS, which the eval
// runner reads, and a hook added later fails here until it is classified.

type Key = keyof Hooks;
const KEYS: { readonly [K in HookName]: Key } = {
  session_start: "sessionStart",
  session_end: "sessionEnd",
  before_input: "beforeInput",
  before_model: "beforeModel",
  after_model: "afterModel",
  before_tool: "beforeTool",
  permission_request: "permissionRequest",
  permission_denied: "permissionDenied",
  after_tool: "afterTool",
  before_tool_result: "beforeToolResult",
  after_tool_batch: "afterToolBatch",
  before_compact: "beforeCompact",
  after_compact: "afterCompact",
  on_stop: "onStop",
  stop_failure: "onStopFailure",
  subagent_start: "subagentStart",
  subagent_stop: "subagentStop",
  before_model_switch: "beforeModelSwitch",
  after_model_switch: "afterModelSwitch",
  notification: "notification",
};

/** Each hook's no-op answer: it lets the step go on and adds nothing. */
const NOOP: { readonly [K in HookName]: unknown } = {
  session_start: [],
  session_end: undefined,
  before_input: { decision: "allow" },
  before_model: { decision: "proceed" },
  after_model: { decision: "proceed" },
  before_tool: { decision: "allow" },
  permission_request: { decision: "allow" },
  permission_denied: undefined,
  after_tool: [],
  before_tool_result: { decision: "proceed" },
  after_tool_batch: [],
  before_compact: { decision: "proceed" },
  after_compact: [],
  on_stop: { decision: "stop" },
  stop_failure: undefined,
  subagent_start: { decision: "allow" },
  subagent_stop: { decision: "stop" },
  before_model_switch: { decision: "allow" },
  after_model_switch: undefined,
  notification: undefined,
};

const overloaded = { error: { reason: "overloaded", http_status: 529 } };
const quick = { base_delay_ms: 1, max_delay_ms: 1 };

/** An extension defining only `hook`, which counts its calls. */
function only(hook: HookName, calls: { n: number }): Extension {
  const fn = async () => {
    calls.n += 1;
    return NOOP[hook];
  };
  return extension({ name: "probe", hooks: { [KEYS[hook]]: fn } });
}

/** The agent run that reaches `hook`, with `ext` as its only extension. */
function runFor(hook: HookName, ext: Extension) {
  const store = sqlite(":memory:");
  const tools = { tools: [lookupOrder] };
  const turn = [use("lookup_order", { id: "1" }, "c1"), say("done")];
  const make = (
    o: Omit<AgentOptions<undefined, string>, "name" | "extensions" | "output">,
  ) => agent({ name: "probe", ...o, extensions: [ext] });
  switch (hook) {
    case "before_tool":
    case "after_tool":
    case "before_tool_result":
    case "after_tool_batch":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: turn }),
          ...tools,
          permissions: { allow: ["lookup_order"] },
        }),
      };
    case "permission_request":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: turn }),
          ...tools,
          permissions: { ask: ["lookup_order"] },
        }),
      };
    case "permission_denied":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: turn }),
          ...tools,
          permissions: { deny: ["lookup_order"] },
        }),
      };
    case "stop_failure":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: [] }),
          retry: { ...quick, max_retries: 0 },
        }),
      };
    case "notification":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: [overloaded, say("done")] }),
          retry: quick,
        }),
      };
    case "before_model_switch":
    case "after_model_switch":
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: [overloaded, overloaded] }),
          fallback: [scriptedModel({ responses: [say("done")] })],
          retry: { ...quick, fallback_after: 2 },
        }),
      };
    case "subagent_start":
    case "subagent_stop": {
      const reviewer = agent({
        name: "reviewer",
        model: scriptedModel({ responses: [say("fine")] }),
      });
      const spawn = use(
        "spawn_agent",
        { agent: "reviewer", prompt: "Review." },
        "c1",
      );
      return {
        store,
        bot: make({
          model: scriptedModel({ responses: [spawn, say("done")] }),
          subagents: [reviewer],
        }),
      };
    }
    default:
      return {
        store,
        bot: make({ model: scriptedModel({ responses: [say("done")] }) }),
      };
  }
}

const decided = (events: readonly KnownEvent[], hook: HookName): boolean =>
  events.some((e) => e.type === "hook_decision" && e.data.hook === hook);

async function probe(hook: HookName): Promise<HookKind | "unreached"> {
  const calls = { n: 0 };
  if (hook === "before_compact" || hook === "after_compact") {
    const h = asked([reply(SUMMARY_TEXT), reply("Answer.")]);
    const wire = {
      name: "probe",
      timeoutMs: 5000,
      hooks: {
        [hook]: async () => {
          calls.n += 1;
          return NOOP[hook];
        },
      },
    };
    const log = await resumeOnce(h, { extensions: [wire] });
    return calls.n === 0
      ? "unreached"
      : decided(log, hook)
        ? "recorded"
        : "observation";
  }
  const { store, bot } = runFor(hook, only(hook, calls));
  const run = await bot.run("Go.", { store });
  const { log } = await openStore(store);
  const read = log.read(run.thread.branch);
  const events = read.ok ? knownEvents(read.value) : [];
  if (calls.n === 0) return "unreached";
  return decided(events, hook) ? "recorded" : "observation";
}

test("every hook's kind, probed on the real loop, is HOOK_KINDS", async () => {
  const probed: Record<string, HookKind | "unreached"> = {};
  for (const hook of HookDecisionData.shape.hook.options)
    probed[hook] = await probe(hook);
  expect(probed).toEqual({ ...HOOK_KINDS });
});
