import { afterEach, expect, test } from "bun:test";
import { ConfigError, sqlite } from "@threads/core";
import { DEFAULT_BUDGET, host } from "../../src";
import { alice, authenticate, say } from "../kit";
import { actor, handoffPair, reader, talker } from "./agents";
import { stopAll } from "./kit";

// host({a2a}) is checked at ready(), beside the channel and schedule checks. Every refusal is
// ConfigError("invalid_config") with a message naming the fix, because each one is something the
// operator changes in the config and nothing a caller can provoke.

afterEach(stopAll);

async function refusal(options: {
  readonly agents: Parameters<typeof host>[0]["agents"];
  readonly expose: Record<string, { readonly description: string }>;
}): Promise<ConfigError> {
  const h = host({
    store: sqlite(":memory:"),
    authenticate,
    agents: options.agents,
    a2a: { expose: options.expose },
  });
  try {
    await h.ready();
  } catch (error) {
    await h.stop();
    if (error instanceof ConfigError) return error;
    throw error;
  }
  await h.stop();
  throw new Error("ready() accepted a config it should have refused");
}

test("an expose key that names no host agent is invalid_config", async () => {
  const error = await refusal({
    agents: { support: talker("hi") },
    expose: { billing: { description: "Billing." } },
  });
  expect(error.code).toBe("invalid_config");
  expect(error.message).toBe("a2a.expose.billing names no host agent");
});

test("an agent with handoffs cannot be exposed", async () => {
  const error = await refusal({
    agents: { support: handoffPair() },
    expose: { support: { description: "Support." } },
  });
  expect(error.code).toBe("invalid_config");
  expect(error.message).toBe(
    "a2a.expose.support: support has handoffs and can't be exposed over A2A; a handoff moves the conversation to another thread, which a remote task can't follow",
  );
});

test("an exposed agent with an acting tool and no approvers is invalid_config", async () => {
  const error = await refusal({
    agents: { support: actor({ responses: [say("hi")] }) },
    expose: { support: { description: "Support." } },
  });
  expect(error.code).toBe("invalid_config");
  expect(error.message).toBe(
    "a2a.expose.support: support is exposed over A2A and has actions that can need approval; add approvers to agent 'support'",
  );
});

test("an agent whose only tools are read_only needs no approvers", async () => {
  const h = host({
    store: sqlite(":memory:"),
    authenticate,
    agents: { support: reader("hi") },
    a2a: { expose: { support: { description: "Reads orders." } } },
  });
  // No throw is the assertion: the approvers rule must not fire on a read-only tool set.
  await h.ready();
  await h.stop();
});

test("the same acting agent is accepted once it declares approvers", async () => {
  const h = host({
    store: sqlite(":memory:"),
    authenticate,
    agents: {
      support: actor({ responses: [say("hi")], approvers: [alice] }),
    },
    a2a: { expose: { support: { description: "Refunds." } } },
  });
  await h.ready();
  await h.stop();
});

test("the default budget is a dollar and ten minutes, stated as a value", () => {
  expect(DEFAULT_BUDGET).toEqual({
    max_cost_nanos: 1_000_000_000,
    max_wall_ms: 600_000,
  });
});

test("the A2A routes serve nothing before ready() has checked the config", async () => {
  const h = host({
    store: sqlite(":memory:"),
    authenticate,
    agents: { support: talker("hi") },
    a2a: { expose: { support: { description: "Support." } } },
  });
  const early = await h.fetch(
    new Request("http://host.test/a2a/support/.well-known/agent-card.json"),
  );
  expect(early.status).toBe(404);
  await h.ready();
  const served = await h.fetch(
    new Request("http://host.test/a2a/support/.well-known/agent-card.json"),
  );
  expect(served.status).toBe(200);
  await h.stop();
});
