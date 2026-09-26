import { CodeSample } from "./code-sample";
import { EVALS } from "./samples";

/*
 * `threads eval` output and the four checks it runs.
 *
 * Line shapes, status words and reason codes are the ones the runner prints (lane 22 §D and the
 * Running evals guide), down to plain left-aligned status words rather than badges. The counts in
 * the summary match the lines above them on purpose.
 */

type Line = {
  readonly status: "PASS" | "FAIL" | "STALE" | "SKIP";
  readonly name: string;
  readonly why?: string;
};

const OUTPUT: readonly Line[] = [
  { status: "PASS", name: "refund-policy" },
  { status: "PASS", name: "duplicate-charge" },
  { status: "PASS", name: "address-change" },
  { status: "FAIL", name: "cancel-order", why: "rerun: unmatched tool_call{name: lookup_order}" },
  { status: "STALE", name: "shipping-eta", why: "drift: prompt, tools (+mcp__jira__create_issue)" },
  { status: "SKIP", name: "handoff-to-eng", why: "offline_not_runnable:child_threads" },
];

/** One hue, four weights: the two that need a person stand out without a second colour. */
const STATUS: Record<Line["status"], string> = {
  PASS: "text-fd-primary",
  FAIL: "font-semibold text-fd-foreground",
  STALE: "text-fd-foreground",
  SKIP: "text-fd-muted-foreground",
};

const CHECKS: readonly { readonly name: string; readonly cost: string; readonly body: string }[] = [
  {
    name: "replay",
    cost: "no model calls",
    body: "Every recorded request in the case re-renders byte for byte under the threads code running now.",
  },
  {
    name: "rerun",
    cost: "no model calls",
    body: "The saved turn still runs to the same events: the same replies, the same tool results, every expectation matched.",
  },
  {
    name: "drift",
    cost: "no model calls",
    body: "The case was recorded with the agent config you have now — the same instructions, tools and model.",
  },
  {
    name: "judge",
    cost: "with --live",
    body: "Your current agent answers the case's input, and a judge model grades the whole turn against your rubric.",
  },
];

export function Evals() {
  return (
    <div className="mt-12">
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.15fr)] lg:gap-8">
        <div className="overflow-hidden rounded-2xl border border-fd-border bg-fd-card">
          <CodeSample sample={EVALS} />
        </div>

        <div className="overflow-hidden rounded-2xl border border-fd-border bg-fd-card">
          <div className="border-b border-fd-border px-4 py-2.5 font-mono text-xs">
            <span className="text-fd-muted-foreground">$ </span>
            <span className="text-fd-foreground">threads eval --agent ./agents.ts</span>
          </div>
          <ol className="px-4 py-3 font-mono text-[0.76rem] leading-6 sm:text-[0.8rem]">
            {OUTPUT.map((l) => (
              <li key={l.name} className="flex flex-wrap items-baseline gap-x-2 py-px">
                <span className={`w-[3.2rem] shrink-0 ${STATUS[l.status]}`}>{l.status}</span>
                <span className="shrink-0 text-fd-foreground">{l.name}</span>
                {l.why ? <span className="min-w-0 break-words text-fd-muted-foreground">{l.why}</span> : null}
              </li>
            ))}
          </ol>
          <p className="border-t border-fd-border px-4 py-3 font-mono text-[0.76rem] text-fd-foreground sm:text-[0.8rem]">
            3 passed, 1 failed, 1 stale, 1 skipped
          </p>
        </div>
      </div>

      <dl className="mt-6 grid gap-px overflow-hidden rounded-2xl border border-fd-border bg-fd-border sm:grid-cols-2 lg:grid-cols-4">
        {CHECKS.map((c) => (
          <div key={c.name} className="bg-fd-card px-4 py-4">
            <dt className="flex flex-wrap items-baseline gap-x-2">
              <span className="font-mono text-[0.85rem] text-fd-primary">{c.name}</span>
              <span className="font-mono text-[0.66rem] text-fd-muted-foreground">{c.cost}</span>
            </dt>
            <dd className="mt-1.5 text-xs leading-5 text-fd-muted-foreground">{c.body}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}
