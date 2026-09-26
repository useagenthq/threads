import { ArrowUpRight } from "lucide-react";
import Image from "next/image";
import Link from "next/link";
import { panel } from "./section";

/*
 * The problem, then the idea, as two different shapes.
 *
 * `Ledger` is the marketing beat the page was missing: the jobs an agent ends up needing, and the
 * thing in threads that already does each one. The right column is only real names — event types
 * from spec/schema/events.v1.schema.json and functions from spec/api.json — so a reader can check
 * every line against the guide it links to.
 *
 * `LogDiagram` is the page's one full-width visual. It carries its own band rather than sitting in
 * a column beside prose, because a section that is mostly one picture is the shape the page needs
 * between two dense ones.
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

export function Ledger() {
  return (
    <ul className={`mt-12 overflow-hidden bg-fd-background ${panel}`}>
      <li className="hidden grid-cols-[minmax(0,1fr)_minmax(0,24rem)] gap-8 border-b border-fd-border bg-fd-primary/[0.06] px-6 py-3 font-mono text-[0.72rem] tracking-[0.16em] uppercase sm:grid">
        <span className="text-fd-muted-foreground">What an agent ends up needing</span>
        <span className="text-fd-primary">What you already have</span>
      </li>
      {NEEDS.map((n) => (
        <li key={n.how} className="border-fd-border not-last:border-b">
          <Link
            href={n.href}
            className="group grid gap-x-8 gap-y-1.5 px-6 py-4 transition-colors hover:bg-fd-card sm:grid-cols-[minmax(0,1fr)_minmax(0,24rem)] sm:items-baseline"
          >
            <span className="text-[0.95rem] leading-6 text-fd-foreground">{n.need}</span>
            <span className="flex items-baseline gap-1.5 font-mono text-[0.8rem] leading-6 text-fd-primary">
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
  );
}

const ALT =
  "Every run is a log: thread_started, user_input, model_request, model_response, tool_call, " +
  "permission_decision, tool_result, turn_completed. Replay (timeline), resume (run again), fork and " +
  "evals (saveCase) are all read from it.";

export function LogDiagram() {
  return (
    <figure className="mx-auto mt-14 w-full max-w-5xl">
      {/* The SVG draws its own rounded card, so a second frame around it would read as a box in a box. */}
      <div className="overflow-hidden rounded-2xl shadow-[var(--shadow-panel)]">
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
      <figcaption className="mx-auto mt-8 max-w-2xl text-center text-base leading-7 text-fd-muted-foreground">
        Replay, resume, fork and evals are not four subsystems. They are four reads of the same events.
      </figcaption>
    </figure>
  );
}
