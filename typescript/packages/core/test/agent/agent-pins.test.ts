import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import { type Agent, agent } from "../../src/agent/agent";
import { hostRunner } from "../../src/agent/registry";
import { ContextPolicy, PermissionsPolicy, RetryPolicy } from "../../src/log";
import { RETRY_DEFAULTS } from "../../src/loop";
import { CONTEXT_DEFAULTS } from "../../src/loop/policy";
import { scriptedModel } from "../../src/model";
import { DEFAULT_PERMISSIONS } from "../../src/permissions";

// The same agent pins the same thread_started and config_hash in both languages
// (spec/schema/README.md, "The pinned config"): the shared vector pins the bytes.

// Each given section is the default one with the vector's fields replaced, as Python takes it;
// the fields alone override the same defaults (policy.ts).
const Fields = z.record(z.string(), z.json()).optional();
const section = <T>(schema: z.ZodType<T>, defaults: T) =>
  Fields.transform((f) =>
    f === undefined ? undefined : schema.parse({ ...defaults, ...f }),
  );
type Def = {
  readonly name: string;
  readonly instructions: string;
  readonly permissions?: z.infer<typeof PermissionsPolicy> | undefined;
  readonly retry?: z.infer<typeof RetryPolicy> | undefined;
  readonly context?: z.infer<typeof ContextPolicy> | undefined;
  readonly subagents?: readonly Def[] | undefined;
  readonly team?: readonly Def[] | undefined;
};
const Def: z.ZodType<Def, unknown> = z.lazy(() =>
  z.strictObject({
    name: z.string(),
    instructions: z.string(),
    permissions: section(PermissionsPolicy, DEFAULT_PERMISSIONS),
    retry: section(RetryPolicy, RETRY_DEFAULTS),
    context: section(ContextPolicy, CONTEXT_DEFAULTS),
    subagents: z.array(Def).optional(),
    team: z.array(Def).optional(),
  }),
);
const Vector = z.strictObject({
  description: z.string(),
  cases: z.array(
    z.strictObject({
      name: z.string(),
      agent: Def,
      thread_started: z.record(z.string(), z.json()),
    }),
  ),
});
const vector = Vector.parse(
  JSON.parse(
    readFileSync(
      join(
        import.meta.dir,
        "../../../../../spec/conformance/vectors/agent-pins.json",
      ),
      "utf8",
    ),
  ),
);

/** The vector's agent. */
function build(d: Def): Agent<never, unknown> {
  const common = {
    name: d.name,
    instructions: d.instructions,
    model: scriptedModel({ responses: [] }),
    ...(d.permissions === undefined ? {} : { permissions: d.permissions }),
    ...(d.retry === undefined ? {} : { retry: d.retry }),
    ...(d.context === undefined ? {} : { context: d.context }),
    ...(d.subagents === undefined ? {} : { subagents: d.subagents.map(build) }),
  };
  return d.team === undefined
    ? agent(common)
    : agent({ ...common, team: d.team.map(build) });
}

describe("agent pins", () => {
  for (const c of vector.cases)
    test(c.name, async () => {
      const started = await hostRunner(build(c.agent))?.started();
      if (started?.type !== "thread_started") throw new Error("no pin");
      // A lead's team ids are fresh per thread.
      const { team: _team, ...data } = started.data;
      expect(z.json().parse(data)).toEqual(c.thread_started);
    });
});
