import { readFileSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import {
  type Agent,
  ConfigError,
  type Live,
  type Model,
  runEvals,
  sqlite,
} from "@threads/core";
import { Budget, canonicalize, caseLine, caseNames } from "@threads/core/host";
import { z } from "zod";

// `threads eval` (spec/api.json cli): runEvals() over saved cases. Without --agent it loads no
// user code and runs the framework checks; --agent adds drift; --live adds the judged run.

export type Io = {
  readonly out: (text: string) => void;
  readonly err: (text: string) => void;
};

export type EvalArgs = {
  readonly agent: string | undefined;
  readonly cases: string;
  readonly only: readonly string[];
  readonly live: boolean;
  readonly store: string | undefined;
  readonly strict: boolean;
  readonly out: string | undefined;
};

type Loaded = {
  readonly agents: readonly Agent<never, unknown>[];
  readonly live?: Live;
};

const isModel = (v: unknown): v is Model =>
  typeof v === "object" &&
  v !== null &&
  "info" in v &&
  "send" in v &&
  typeof v.send === "function";

const isAgent = (v: unknown): v is Agent<never, unknown> =>
  typeof v === "object" &&
  v !== null &&
  "run" in v &&
  typeof v.run === "function" &&
  "name" in v &&
  typeof v.name === "string";

function exported(module: object, name: string): unknown {
  return name in module ? Reflect.get(module, name) : undefined;
}

/** The module's agents, and for --live its judge, budget and optional rubric. */
function fromModule(
  module: object,
  path: string,
  live: boolean,
): Loaded | string {
  const main = exported(module, "default");
  const agents = (Array.isArray(main) ? main : [main]).filter(isAgent);
  if (agents.length === 0)
    return `export default an agent or a list of agents from ${path}`;
  if (!live) return { agents };
  const judge = exported(module, "judge");
  if (!isModel(judge)) return `export judge from ${path}`;
  const budget = Budget.safeParse(exported(module, "budget"));
  if (!budget.success)
    return `export budget from ${path} (a Budget, such as {max_cost_nanos: 500_000_000})`;
  const rubric = z
    .array(z.string())
    .optional()
    .safeParse(exported(module, "rubric"));
  if (!rubric.success)
    return `rubric exported from ${path} must be a list of criteria`;
  const user = exported(module, "user");
  if (user !== undefined && !isModel(user))
    return `user exported from ${path} must be a model`;
  return {
    agents,
    live: {
      judge,
      budget: budget.data,
      ...(rubric.data === undefined ? {} : { rubric: rubric.data }),
      ...(user === undefined ? {} : { user }),
    },
  };
}

async function load(args: EvalArgs, io: Io): Promise<Loaded | number> {
  if (args.agent === undefined) {
    if (!args.live) return { agents: [] };
    io.err("threads eval --live needs --agent <module>\n");
    return 2;
  }
  const path = resolve(args.agent);
  let module: unknown;
  try {
    module = await import(path);
  } catch (error) {
    io.err(
      `${path}: import failed: ${error instanceof Error ? error.message : String(error)}\n`,
    );
    return 2;
  }
  const loaded =
    typeof module === "object" && module !== null
      ? fromModule(module, args.agent, args.live)
      : `${path} is not a module`;
  if (typeof loaded === "string") {
    io.err(`${loaded}\n`);
    return 2;
  }
  return loaded;
}

function canonical(value: unknown): string {
  const text = canonicalize(JSON.parse(JSON.stringify(value)));
  if (!text.ok) throw new Error("a report is canonical JSON");
  return text.value;
}

const Simulated = z.looseObject({
  simulate: z
    .union([
      z.looseObject({
        kind: z.literal("model"),
        max_messages: z.int().optional(),
      }),
      z.looseObject({
        kind: z.literal("script"),
        messages: z.array(z.string()),
      }),
    ])
    .optional(),
});

/** The `simulate` of each case, read off case.json: what the preflight line counts. */
function simulations(
  cases: string,
  only: readonly string[],
): readonly NonNullable<z.infer<typeof Simulated>["simulate"]>[] {
  return caseNames(cases)
    .filter((c) => only.length === 0 || only.includes(c))
    .flatMap((name) => {
      try {
        const meta = Simulated.safeParse(
          JSON.parse(readFileSync(resolve(cases, name, "case.json"), "utf8")),
        );
        return meta.success && meta.data.simulate !== undefined
          ? [meta.data.simulate]
          : [];
      } catch {
        return [];
      }
    });
}

/** What a live run will cost, printed before the first model call; 2 when a model is missing. */
function preflight(args: EvalArgs, live: Live, io: Io): number | undefined {
  const n = caseNames(args.cases).filter(
    (c) => args.only.length === 0 || args.only.includes(c),
  ).length;
  const sims = simulations(args.cases, args.only);
  if (sims.some((s) => s.kind === "model") && live.user === undefined) {
    io.err(`export user from ${args.agent ?? "./agents.ts"}\n`);
    return 2;
  }
  const messages = sims.reduce(
    (total, s) =>
      total +
      (s.kind === "model" ? (s.max_messages ?? 5) : s.messages.length + 1),
    0,
  );
  io.out(
    sims.length === 0
      ? `live: ${n} cases, up to ${2 * n} model runs (agent + judge), budget ${canonical(live.budget)} per run\n`
      : `live: ${n} cases (${sims.length} simulated, up to ${messages} user messages), budget ${canonical(live.budget)} per conversation and per judge run\n`,
  );
  return undefined;
}

type Report = Awaited<ReturnType<typeof runEvals>>;

/** The run, or exit 2 when the options are a setup mistake. */
async function report(
  args: EvalArgs,
  loaded: Loaded,
  io: Io,
): Promise<Report | number> {
  try {
    return await runEvals({
      cases: args.cases,
      ...(args.only.length === 0 ? {} : { only: args.only }),
      ...(args.agent === undefined ? {} : { agents: loaded.agents }),
      ...(loaded.live === undefined ? {} : { live: loaded.live }),
      ...(args.store === undefined ? {} : { store: sqlite(args.store) }),
      strict: args.strict,
    });
  } catch (error) {
    if (!(error instanceof ConfigError)) throw error;
    io.err(`${error.code}: ${error.message}\n`);
    return 2;
  }
}

export async function evals(args: EvalArgs, io: Io): Promise<number> {
  const loaded = await load(args, io);
  if (typeof loaded === "number") return loaded;
  const { live } = loaded;
  if (live !== undefined) {
    const stop = preflight(args, live, io);
    if (stop !== undefined) return stop;
  }
  const ran = await report(args, loaded, io);
  if (typeof ran === "number") return ran;
  for (const c of ran.cases) io.out(`${caseLine(c)}\n`);
  if (live !== undefined && args.store === undefined)
    io.out("judge threads were not kept; pass --store to keep them\n");
  io.out(`${ran.summary}\n`);
  if (args.out !== undefined) writeFileSync(args.out, `${canonical(ran)}\n`);
  return ran.ok ? 0 : 1;
}
