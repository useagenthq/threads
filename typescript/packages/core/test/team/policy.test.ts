import { describe, expect, test } from "bun:test";
import { ConfigError } from "../../src/agent/errors";
import {
  checkMessagePolicy,
  leads,
  type MessagePolicyRule,
  ruleFor,
  ruleStarts,
  rulesFrom,
  startable,
  teamTools,
} from "../../src/team/policy";

// The host's messagePolicy as pure rules (spec/api.json host.message_policy): what an agent may
// do, which team tools that gives it, whether it is a lead, and the per-hop budget minimum.

const agents = new Set(["support", "billing", "hr"]);

const rule = (
  from: string,
  to: string,
  allow: MessagePolicyRule["allow"],
): MessagePolicyRule => ({ from, to, allow });

describe("the host's messagePolicy at setup", () => {
  test("a from or to that names no host agent is invalid_config", () => {
    for (const bad of [
      rule("nobody", "billing", ["ask"]),
      rule("support", "nobody", ["ask"]),
    ]) {
      let thrown: unknown;
      try {
        checkMessagePolicy([bad], agents);
      } catch (error) {
        thrown = error;
      }
      expect(thrown).toBeInstanceOf(ConfigError);
      expect(thrown instanceof ConfigError && thrown.code).toBe(
        "invalid_config",
      );
      expect(thrown instanceof ConfigError && thrown.message).toContain(
        "is not a host agent",
      );
    }
  });

  test("an empty allow is invalid_config naming the rule", () => {
    let thrown: unknown;
    try {
      checkMessagePolicy([rule("support", "billing", [])], agents);
    } catch (error) {
      thrown = error;
    }
    expect(thrown instanceof ConfigError && thrown.code).toBe("invalid_config");
    expect(thrown instanceof ConfigError && thrown.message).toBe(
      "messagePolicy rule support -> billing: allow is empty; list what support may do, or drop the rule",
    );
  });

  test("a repeated (from, to) pair is duplicate_name", () => {
    let thrown: unknown;
    try {
      checkMessagePolicy(
        [
          rule("support", "billing", ["ask"]),
          rule("support", "billing", ["send"]),
        ],
        agents,
      );
    } catch (error) {
      thrown = error;
    }
    expect(thrown instanceof ConfigError && thrown.code).toBe("duplicate_name");
    expect(thrown instanceof ConfigError && thrown.message).toContain(
      "is listed twice",
    );
  });

  test("the same pair in each direction, and two pairs of one from, are fine", () => {
    expect(() =>
      checkMessagePolicy(
        [
          rule("support", "billing", ["ask"]),
          rule("billing", "support", ["send"]),
          rule("support", "hr", ["ask"]),
        ],
        agents,
      ),
    ).not.toThrow();
  });
});

describe("what an agent's rules give it", () => {
  const rules = [
    rule("support", "billing", ["ask"]),
    rule("support", "hr", ["monitor"]),
    rule("hr", "billing", ["start", "send"]),
  ];

  test("only the rules with the agent as from", () => {
    expect(rulesFrom(rules, "support").map((r) => r.to)).toEqual([
      "billing",
      "hr",
    ]);
    expect(rulesFrom(rules, "billing")).toEqual([]);
  });

  test("one team tool per allowed op, and wait comes with monitor", () => {
    const of = (agent: string) =>
      teamTools(undefined, false, rulesFrom(rules, agent)).toSorted();
    expect(of("support")).toEqual(["ask", "monitor", "wait"]);
    expect(of("hr")).toEqual(["send", "start"]);
    expect(teamTools(undefined, false, [])).toEqual([]);
  });

  test("a lead and a member get all seven, whatever their rules say", () => {
    const seven = [
      "ask",
      "cancel",
      "monitor",
      "reply",
      "send",
      "start",
      "wait",
    ];
    expect(teamTools([], false, []).toSorted()).toEqual(seven);
    expect(teamTools(undefined, true, []).toSorted()).toEqual(seven);
  });

  test("a rule allowing start makes its from a lead; ask alone does not", () => {
    expect(leads(undefined, rulesFrom(rules, "hr"))).toBe(true);
    expect(leads(undefined, rulesFrom(rules, "support"))).toBe(false);
    // An agent with a team of its own is a lead whatever its rules say.
    expect(leads([], rulesFrom(rules, "support"))).toBe(true);
  });

  test("start may name the team's agents, then the ones a start rule adds, each once", () => {
    expect(ruleStarts(rulesFrom(rules, "hr"))).toEqual(["billing"]);
    expect(startable(["billing"], rulesFrom(rules, "hr"))).toEqual(["billing"]);
    expect(startable(["support"], rulesFrom(rules, "hr"))).toEqual([
      "support",
      "billing",
    ]);
    expect(startable(undefined, rulesFrom(rules, "support"))).toEqual([]);
  });

  test("a rule is found by from, to and op, never by an op it omits", () => {
    expect(ruleFor(rules, "support", "billing", "ask")?.to).toBe("billing");
    expect(ruleFor(rules, "support", "billing", "send")).toBeUndefined();
    expect(ruleFor(rules, "billing", "support", "ask")).toBeUndefined();
  });
});
