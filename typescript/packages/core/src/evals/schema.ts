import { z } from "zod";
import { Cost, Int, JsonValue, PosInt } from "../log";
import type { Arr, EnumOf, Lit, Opt, Strict } from "../log/zod-types";

// The eval runner's wire shapes (spec/schema/eval.v1.schema.json): the judge's verdicts, the
// judge input it grades, and the report runEvals returns and `threads eval --out` writes. The
// report has no timestamps or durations, so an offline report is byte-stable in both languages.

export const Verdict: Strict<{
  criterion: typeof PosInt;
  pass: z.ZodBoolean;
  reason: z.ZodString;
}> = z
  .strictObject({
    criterion: PosInt.describe("The rubric criterion, numbered from 1."),
    pass: z.boolean(),
    reason: z.string().min(1).max(500),
  })
  .meta({ id: "Verdict" });
export type Verdict = z.infer<typeof Verdict>;

/** The judge's structured output: exactly one verdict per criterion, 1..n in order. */
export const Verdicts: Strict<{ verdicts: Arr<typeof Verdict> }> = z
  .strictObject({ verdicts: z.array(Verdict) })
  .meta({ id: "Verdicts" });
export type Verdicts = z.infer<typeof Verdicts>;

/** Plain-English pass/fail criteria a live eval's judge checks. */
export const Rubric: z.ZodArray<z.ZodString> = z
  .array(z.string().min(1).max(500))
  .min(1)
  .max(20)
  .meta({ id: "Rubric" });

const ToolCallItem: Strict<{
  kind: Lit<"tool_call">;
  name: z.ZodString;
  input: typeof JsonValue;
}> = z.strictObject({
  kind: z.literal("tool_call"),
  name: z.string(),
  input: JsonValue.describe(
    "The call's input, or its canonical JSON cut at 4,000 code points when longer.",
  ),
});
const ToolResultItem: Strict<{
  kind: Lit<"tool_result">;
  name: z.ZodString;
  is_error: z.ZodBoolean;
  text: z.ZodString;
}> = z.strictObject({
  kind: z.literal("tool_result"),
  name: z.string(),
  is_error: z.boolean(),
  text: z.string(),
});
const AssistantItem: Strict<{ kind: Lit<"assistant">; text: z.ZodString }> =
  z.strictObject({ kind: z.literal("assistant"), text: z.string() });
const UserItem: Strict<{ kind: Lit<"user">; text: z.ZodString }> =
  z.strictObject({ kind: z.literal("user"), text: z.string() });
const OmittedItem: Strict<{ kind: Lit<"omitted">; count: typeof PosInt }> =
  z.strictObject({ kind: z.literal("omitted"), count: PosInt });

export const TranscriptItem: z.ZodDiscriminatedUnion<
  [
    typeof ToolCallItem,
    typeof ToolResultItem,
    typeof AssistantItem,
    typeof UserItem,
    typeof OmittedItem,
  ],
  "kind"
> = z
  .discriminatedUnion("kind", [
    ToolCallItem,
    ToolResultItem,
    AssistantItem,
    UserItem,
    OmittedItem,
  ])
  .meta({ id: "TranscriptItem" });
export type TranscriptItem = z.infer<typeof TranscriptItem>;

/** The judge thread's user_input text, as RFC 8785 canonical JSON: the task stays data. */
export const JudgeInput: Strict<{
  answer: typeof JsonValue;
  goal: Opt<z.ZodString>;
  rubric: typeof Rubric;
  task: z.ZodString;
  transcript: Arr<typeof TranscriptItem>;
}> = z
  .strictObject({
    answer: JsonValue,
    goal: z
      .string()
      .describe(
        "The simulated user's goal; absent unless the case simulates a model user.",
      )
      .optional(),
    rubric: Rubric,
    task: z.string(),
    transcript: z.array(TranscriptItem),
  })
  .meta({ id: "JudgeInput" });
export type JudgeInput = z.infer<typeof JudgeInput>;

/** The simulated user's structured output each turn (spec lane 32, C). */
export const UserTurn: Strict<{
  message: z.ZodString;
  done: z.ZodBoolean;
}> = z
  .strictObject({
    message: z
      .string()
      .max(4000)
      .describe("The next message the user would send; empty only when done."),
    done: z
      .boolean()
      .describe(
        "The goal is met or clearly can't be met; the message is not sent.",
      ),
  })
  .meta({ id: "UserTurn" });
export type UserTurn = z.infer<typeof UserTurn>;

/** case.schema.json EventMatcher: exact on the listed envelope keys, deep subset on data. */
export const EventMatcher: Strict<{
  type: z.ZodString;
  seq: Opt<typeof PosInt>;
  actor_kind: Opt<z.ZodString>;
  epoch: Opt<z.ZodInt>;
  branch_id: Opt<z.ZodString>;
  critical: Opt<z.ZodBoolean>;
  data: Opt<z.ZodRecord<z.ZodString, typeof JsonValue>>;
}> = z
  .strictObject({
    type: z.string(),
    seq: PosInt.optional(),
    actor_kind: z.string().optional(),
    epoch: z.int().optional(),
    branch_id: z.string().optional(),
    critical: z.boolean().optional(),
    data: z.record(z.string(), JsonValue).optional(),
  })
  .meta({ id: "EventMatcher" });
export type EventMatcher = z.infer<typeof EventMatcher>;

const ReplayCheck: z.ZodDiscriminatedUnion<
  [
    Strict<{ ok: Lit<true> }>,
    Strict<{ ok: Lit<false>; code: z.ZodString; seq: Opt<typeof Int> }>,
  ],
  "ok"
> = z.discriminatedUnion("ok", [
  z.strictObject({ ok: z.literal(true) }),
  z.strictObject({
    ok: z.literal(false),
    code: z.string(),
    seq: Int.optional(),
  }),
]);

const Mismatch: Strict<{
  index: typeof Int;
  want: z.ZodNullable<z.ZodString>;
  got: z.ZodNullable<z.ZodString>;
  hint: Opt<z.ZodString>;
}> = z.strictObject({
  index: Int.describe("The first appended event that differs, from 0."),
  want: z.string().nullable().describe("The recorded event's type."),
  got: z.string().nullable().describe("The rerun's event type."),
  hint: z.string().optional(),
});

const RerunCheck: Strict<{
  ok: z.ZodBoolean;
  unmatched: Arr<typeof EventMatcher>;
  script_left: typeof Int;
  stubs_unmatched: typeof Int;
  unrecorded_calls: typeof Int;
  unrecorded_hooks: typeof Int;
  mismatch: Opt<typeof Mismatch>;
}> = z.strictObject({
  ok: z.boolean(),
  unmatched: z.array(EventMatcher),
  script_left: Int,
  stubs_unmatched: Int,
  unrecorded_calls: Int,
  unrecorded_hooks: Int,
  mismatch: Mismatch.optional(),
});

const DRIFT_KINDS = ["prompt", "tools", "model", "config"] as const;
const ToolDiff: Strict<{
  added: Arr<z.ZodString>;
  removed: Arr<z.ZodString>;
  changed: Arr<z.ZodString>;
}> = z.strictObject({
  added: z.array(z.string()),
  removed: z.array(z.string()),
  changed: z.array(z.string()),
});
export type ToolDiff = z.infer<typeof ToolDiff>;

const DriftCheck: Strict<{
  ok: z.ZodBoolean;
  kinds: Arr<EnumOf<typeof DRIFT_KINDS>>;
  tools: Opt<typeof ToolDiff>;
  unchecked: Opt<Arr<z.ZodString>>;
  agents: Opt<Arr<z.ZodString>>;
}> = z.strictObject({
  ok: z.boolean(),
  kinds: z.array(z.enum(DRIFT_KINDS)),
  tools: ToolDiff.optional(),
  unchecked: z
    .array(z.string())
    .describe(
      "What the dry pin couldn't compare (mcp:<server>, extension:<name>, ...).",
    )
    .optional(),
  agents: z
    .array(z.string())
    .describe("agent_not_found: the agent names that were given.")
    .optional(),
});
export type DriftCheck = z.infer<typeof DriftCheck>;

const GradedVerdict: Strict<{
  criterion: typeof PosInt;
  text: z.ZodString;
  pass: z.ZodBoolean;
  reason: z.ZodString;
}> = z.strictObject({
  criterion: PosInt,
  text: z.string(),
  pass: z.boolean(),
  reason: z.string(),
});

const JudgeCheck: Strict<{
  answer: typeof JsonValue;
  transcript_items: typeof Int;
  score: z.ZodNumber;
  fresh_sandbox: z.ZodBoolean;
  verdicts: Arr<typeof GradedVerdict>;
  thread_id: Opt<z.ZodString>;
  judge_thread_id: Opt<z.ZodString>;
}> = z.strictObject({
  answer: JsonValue,
  transcript_items: Int,
  score: z.number().min(0).max(1),
  fresh_sandbox: z.boolean(),
  verdicts: z.array(GradedVerdict),
  thread_id: z.string().optional(),
  judge_thread_id: z.string().optional(),
});
export type JudgeCheck = z.infer<typeof JudgeCheck>;

const STATUSES = [
  "passed",
  "failed",
  "stale",
  "skipped",
  "error",
  "not_run",
] as const;

const PREFIXES = ["continued", "redriven", "none"] as const;
const ENDINGS = ["user_done", "script_done", "max_messages"] as const;

/** What a simulated case's live conversation did (spec lane 32, E). */
const Simulation: Strict<{
  messages: typeof Int;
  prefix: EnumOf<typeof PREFIXES>;
  prefix_turns: typeof Int;
  ended: EnumOf<typeof ENDINGS>;
  user_thread_id: Opt<z.ZodString>;
}> = z.strictObject({
  messages: Int.describe("User messages sent, the opener included."),
  prefix: z
    .enum(PREFIXES)
    .describe(
      "continued: the case log was imported and the thread went on. redriven: the prefix inputs were re-sent to the changed agent. none: the saved turn was the first.",
    ),
  prefix_turns: Int.describe("Recorded inputs re-sent before the opener."),
  ended: z.enum(ENDINGS),
  user_thread_id: z
    .string()
    .describe("The simulated user's thread; kept only with a persistent store.")
    .optional(),
});
export type Simulation = z.infer<typeof Simulation>;

export const EvalCaseResult: Strict<{
  name: z.ZodString;
  status: EnumOf<typeof STATUSES>;
  reason: Opt<z.ZodString>;
  simulation: Opt<typeof Simulation>;
  checks: Strict<{
    replay: Opt<typeof ReplayCheck>;
    rerun: Opt<typeof RerunCheck>;
    drift: Opt<typeof DriftCheck>;
    judge: Opt<typeof JudgeCheck>;
  }>;
}> = z
  .strictObject({
    name: z.string(),
    status: z.enum(STATUSES),
    reason: z.string().optional(),
    simulation: Simulation.optional(),
    checks: z.strictObject({
      replay: ReplayCheck.optional(),
      rerun: RerunCheck.optional(),
      drift: DriftCheck.optional(),
      judge: JudgeCheck.optional(),
    }),
  })
  .meta({ id: "EvalCaseResult" });
export type EvalCaseResult = z.infer<typeof EvalCaseResult>;
export type CaseStatus = EvalCaseResult["status"];
export type Checks = EvalCaseResult["checks"];

export const EvalReport: Strict<{
  format: Lit<"threads-eval">;
  format_version: Lit<1>;
  summary: z.ZodString;
  ok: z.ZodBoolean;
  passed: typeof Int;
  failed: typeof Int;
  stale: typeof Int;
  skipped: typeof Int;
  errors: typeof Int;
  not_run: typeof Int;
  aborted: Opt<
    Strict<{
      code: Lit<"model_blocked">;
      case: z.ZodString;
      model: z.ZodString;
    }>
  >;
  model_calls: Strict<{
    agent: typeof Int;
    user: typeof Int;
    judge: typeof Int;
  }>;
  cost: z.ZodNullable<typeof Cost>;
  cases: Arr<typeof EvalCaseResult>;
}> = z
  .strictObject({
    format: z.literal("threads-eval"),
    format_version: z.literal(1),
    summary: z.string().describe("The one line the CLI prints last."),
    ok: z
      .boolean()
      .describe(
        "The run passed: no case failed, errored or went unrun, and under strict none is stale or skipped. `threads eval` exits 1 when false.",
      ),
    passed: Int,
    failed: Int,
    stale: Int,
    skipped: Int,
    errors: Int,
    not_run: Int,
    aborted: z
      .strictObject({
        code: z.literal("model_blocked"),
        case: z.string(),
        model: z.string(),
      })
      .describe(
        "The model-request guard blocked a live model: the run stopped, the rest are not_run.",
      )
      .optional(),
    model_calls: z.strictObject({
      agent: Int,
      user: Int.describe("Runs of the simulated user (spec lane 32)."),
      judge: Int,
    }),
    cost: Cost.nullable().describe("null: some live usage had no price."),
    cases: z.array(EvalCaseResult),
  })
  .meta({ id: "EvalReport" });
export type EvalReport = z.infer<typeof EvalReport>;
