import { ArrowUpRight } from "lucide-react";
import Image from "next/image";
import Link from "next/link";

/*
 * The problem, then the idea, in one band.
 *
 * The ledger is the marketing beat the page was missing: the jobs an agent ends up needing, and
 * the thing in threads that already does each one. The right column is only real names — event
 * types from spec/schema/events.v1.schema.json and functions from spec/api.json — so a reader can
 * check every line against the docs it links to.
 *
 * The diagram underneath is the page's first visual anchor: it says state = reduce(log) in one
 * picture, which is what makes every section after it possible.
 */

type Need = { readonly need: string; readonly how: string; readonly href: string };

const NEEDS: readonly Need[] = [
  {
    need: "Carry on after a crash without paying the refund twice",
    how: "effect_begin → effect_commit",
    href: "/docs/production/durability",
  },
  {
    need: "Hold a risky call until a person decides, hours later, in another process",
    how: "approval_requested → approval_granted",
    href: "/docs/control/human-in-the-loop",
  },
  {
    need: "Show the exact bytes the model was sent, months after the answer went out",
    how: "model_request.request_ref",
    href: "/docs/evals/timeline",
  },
  {
    need: "Give the agent a machine of its own, with no internet and none of your keys",
    how: "sandbox: e2b()",
    href: "/docs/sandboxes/overview",
  },
  {
    need: "Turn a real conversation into a test that reruns with no model calls",
    how: "saveCase() → threads eval",
    href: "/docs/evals/saved-cases",
  },
  {
    need: "Open a run as a trace in the tracing tool you already pay for",
    how: "otel()",
    href: "/docs/production/observability",
  },
];

const ALT =
  "Every run is a log: thread_started, user_input, model_request, model_response, tool_call, " +
  "permission_decision, tool_result, turn_completed. Replay (timeline), resume (run again), fork and " +
  "evals (saveCase) are all read from it.";

export function Plumbing() {
  return (
    <div className="mt-12">
      <ul className="overflow-hidden rounded-2xl border border-fd-border bg-fd-background">
        <li className="hidden grid-cols-[minmax(0,1fr)_minmax(0,20rem)] gap-6 border-b border-fd-border px-5 py-2.5 font-mono text-[0.68rem] tracking-widest text-fd-muted-foreground uppercase sm:grid">
          <span>What an agent ends up needing</span>
          <span>What you already have</span>
        </li>
        {NEEDS.map((n) => (
          <li key={n.how} className="border-fd-border not-last:border-b">
            <Link
              href={n.href}
              className="group grid gap-x-6 gap-y-1.5 px-5 py-4 transition-colors hover:bg-fd-card sm:grid-cols-[minmax(0,1fr)_minmax(0,20rem)] sm:items-baseline"
            >
              <span className="text-sm leading-6 text-fd-foreground">{n.need}</span>
              <span className="flex items-baseline gap-1.5 font-mono text-[0.78rem] leading-6 text-fd-primary">
                <span className="min-w-0 break-words">{n.how}</span>
                <ArrowUpRight
                  className="size-3.5 shrink-0 self-center opacity-0 transition-opacity group-hover:opacity-100"
                  aria-hidden="true"
                />
              </span>
            </Link>
          </li>
        ))}
      </ul>

      <figure className="mt-10 grid items-center gap-8 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)] lg:gap-12">
        <div className="min-w-0 overflow-hidden rounded-2xl border border-fd-border bg-fd-background p-3 sm:p-5">
          <Image
            src="/images/run-is-a-log-light.svg"
            alt={ALT}
            width={880}
            height={516}
            unoptimized
            fetchPriority="high"
            className="h-auto w-full dark:hidden"
          />
          <Image
            src="/images/run-is-a-log-dark.svg"
            alt={ALT}
            width={880}
            height={516}
            unoptimized
            fetchPriority="high"
            className="hidden h-auto w-full dark:block"
          />
        </div>
        <figcaption className="text-sm leading-7 text-fd-muted-foreground">
          <span className="block font-mono text-lg text-fd-foreground sm:text-xl">state = reduce(log)</span>
          <span className="mt-3 block">
            Nothing is kept beside the log, so nothing can drift from it. Replay, resume, fork and evals are
            not four subsystems: they are four reads of the same events.
          </span>
        </figcaption>
      </figure>
    </div>
  );
}
