import type { Cost } from "../log";
import type { CaseStatus, EvalCaseResult, EvalReport } from "./schema";

// The report runEvals returns (spec lane 22, D): counts, the one summary line the CLI prints,
// and whether the run passed. No timestamps or durations, so an offline report is byte-stable
// and the same in both languages.

export type Totals = {
  readonly calls: { readonly agent: number; readonly judge: number };
  readonly cost: Cost | null;
  readonly live: boolean;
  /** Drift ran: agents were given. */
  readonly agents: boolean;
  readonly strict: boolean;
  readonly aborted?: NonNullable<EvalReport["aborted"]>;
};

const FRAMEWORK_ONLY =
  " (framework checks only; pass --agent to detect changes to your agents)";

/** Two costs added; null once either part ran unpriced, `total` undefined before any part. */
export function addCost(
  total: Cost | null | undefined,
  part: Cost | null,
): Cost | null {
  if (total === null || part === null) return null;
  if (total === undefined) return part;
  return {
    currency: total.currency,
    known_nanos: total.known_nanos + part.known_nanos,
    upper_bound_nanos: total.upper_bound_nanos + part.upper_bound_nanos,
    complete: total.complete && part.complete,
    bounded: total.bounded && part.bounded,
  };
}

/** Nano-dollars as dollars and cents, rounded half up. */
export function dollars(nanos: number): string {
  const cents = Math.floor((nanos + 5_000_000) / 10_000_000);
  return `$${Math.floor(cents / 100)}.${String(cents % 100).padStart(2, "0")}`;
}

const count = (cases: readonly EvalCaseResult[], status: CaseStatus): number =>
  cases.filter((c) => c.status === status).length;

function summaryOf(r: Omit<EvalReport, "summary" | "ok">, t: Totals): string {
  const parts = [`${r.passed} passed`, `${r.failed} failed`];
  if (r.stale > 0) parts.push(`${r.stale} stale`);
  if (r.skipped > 0) parts.push(`${r.skipped} skipped`);
  if (r.errors > 0) parts.push(`${r.errors} error${r.errors === 1 ? "" : "s"}`);
  if (r.not_run > 0) parts.push(`${r.not_run} not run`);
  let line = parts.join(", ");
  if (t.live) {
    const { agent, judge } = t.calls;
    line += `; ${agent + judge} model calls (${agent} agent, ${judge} judge), ${
      t.cost === null ? "cost unknown" : dollars(t.cost.known_nanos)
    }`;
  }
  if (t.aborted !== undefined)
    line += `; aborted: ${t.aborted.code} (${t.aborted.model})`;
  return t.agents ? line : line + FRAMEWORK_ONLY;
}

export function report(
  cases: readonly EvalCaseResult[],
  t: Totals,
): EvalReport {
  const counts = {
    passed: count(cases, "passed"),
    failed: count(cases, "failed"),
    stale: count(cases, "stale"),
    skipped: count(cases, "skipped"),
    errors: count(cases, "error"),
    not_run: count(cases, "not_run"),
  };
  const body = {
    format: "threads-eval" as const,
    format_version: 1 as const,
    ...counts,
    ...(t.aborted === undefined ? {} : { aborted: t.aborted }),
    model_calls: t.calls,
    cost: t.cost,
    cases: [...cases],
  };
  const failed = counts.failed + counts.errors + counts.not_run > 0;
  const strictly = t.strict && counts.stale + counts.skipped > 0;
  return {
    ...body,
    summary: summaryOf(body, t),
    ok: !failed && !strictly && t.aborted === undefined,
  };
}

/** One line per case, as the CLI prints them. */
export function caseLine(c: EvalCaseResult): string {
  const label = {
    passed: "PASS",
    failed: "FAIL",
    stale: "STALE",
    skipped: "SKIP",
    error: "ERROR",
    not_run: "NOT RUN",
  }[c.status];
  const unchecked = c.checks.drift?.unchecked?.join(", ");
  // After a reason: "STALE c drift: model; unchecked mcp:jira". Alone: "PASS c (unchecked ...)".
  if (c.reason === undefined)
    return `${label} ${c.name}${unchecked === undefined ? "" : ` (drift: unchecked ${unchecked})`}`;
  return `${label} ${c.name} ${c.reason}${unchecked === undefined ? "" : `; unchecked ${unchecked}`}`;
}
