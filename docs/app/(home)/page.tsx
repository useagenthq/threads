import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { CodeSample } from "@/components/home/code-sample";
import { CopyCommand } from "@/components/home/copy-command";
import { Evals } from "@/components/home/evals";
import { Features } from "@/components/home/features";
import { GitHubIcon } from "@/components/home/github-icon";
import { HeroThreads } from "@/components/home/hero-threads";
import { Ledger, LogDiagram } from "@/components/home/plumbing";
import { Providers } from "@/components/home/providers";
import { TELEMETRY } from "@/components/home/samples";
import { Band, Code, Eyebrow, panel, SectionHeading } from "@/components/home/section";
import { Timeline } from "@/components/home/timeline";
import { Backends, Trace } from "@/components/home/trace";
import { UseCases } from "@/components/home/use-cases";
import { githubUrl } from "@/lib/shared";

const buttonBase =
  "inline-flex items-center justify-center gap-2 rounded-full text-sm font-medium transition-colors";

/*
 * The argument, in order: the claim → the models it runs on → the plumbing you stop writing →
 * why that works, in one picture → one run read three ways (log, trace, eval) → what you build →
 * start.
 *
 * Each beat is a different shape on purpose. A statement with air, a bordered rail, a two-column
 * ledger, one full-width picture, one very dense panel, an asymmetric split, a tab set, a grid,
 * and a statement again. Surfaces alternate between the page colour, a raised card and a sunken
 * well, so nothing sits on the same depth as the thing above it.
 */
export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col">
      <Hero />
      <Providers />
      <StopRebuilding />
      <TheIdea />
      <TheRecord />
      <Observability />
      <EvalsSection />
      <BuiltFor />
      <WhatYouCanBuild />
      <CallToAction />
    </main>
  );
}

/** Real, checkable facts. No counts of users, stars or customers: we do not have any to quote. */
const FACTS = [
  ["TypeScript and Python", "one log, both languages"],
  ["73 event types", "in the v1 schema"],
  ["SQLite or Postgres", "your database, your data"],
  ["Apache-2.0", "open source"],
] as const;

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-fd-border">
      <HeroThreads />
      <div className="relative mx-auto flex max-w-[80rem] flex-col items-center px-4 pt-20 pb-16 text-center sm:px-6 sm:pt-28 lg:pt-32 lg:pb-24">
        <Link
          href={githubUrl}
          className="mb-8 inline-flex items-center gap-2.5 rounded-full border border-fd-primary/30 bg-fd-background/80 py-1.5 pr-4 pl-3 text-sm font-medium text-fd-foreground shadow-[var(--shadow-panel)] backdrop-blur transition-colors hover:border-fd-primary/60"
        >
          <span className="size-2 rounded-full bg-fd-primary" aria-hidden="true" />
          Alpha · Open source on GitHub
          <ArrowRight className="size-3.5 text-fd-muted-foreground" aria-hidden="true" />
        </Link>
        <h1 className="max-w-4xl text-[2.75rem] leading-[1.04] font-semibold tracking-[-0.03em] text-balance sm:text-6xl lg:text-7xl">
          Agents you can <span className="text-fd-primary">inspect</span>,{" "}
          <span className="text-fd-primary">replay</span> and <span className="text-fd-primary">trust</span>.
        </h1>
        <p className="mt-7 max-w-2xl text-lg leading-8 text-pretty text-fd-muted-foreground">
          Build in TypeScript or Python on an append-only event log. Inspect model calls, tool effects and
          approvals; use built-in sandboxes, channels, memory and evals.
        </p>
        <div className="mt-9 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/docs/quickstart"
            className={`${buttonBase} h-11 bg-fd-primary px-6 text-fd-primary-foreground shadow-[var(--shadow-panel)] hover:bg-fd-primary/90`}
          >
            Get started
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
          <Link
            href={githubUrl}
            className={`${buttonBase} h-11 border border-fd-border bg-fd-background px-5 hover:bg-fd-accent hover:text-fd-accent-foreground`}
          >
            <GitHubIcon className="size-4" />
            GitHub
          </Link>
        </div>
        <div className="mt-7 flex w-full max-w-full flex-col items-center gap-2">
          <CopyCommand command="git clone https://github.com/useagenthq/threads" />
          <p className="text-xs text-fd-muted-foreground">
            Not on npm or PyPI yet.{" "}
            <Link href="/docs/installation" className="underline underline-offset-4 hover:text-fd-foreground">
              Install from source
            </Link>
            .
          </p>
        </div>

        <dl className="mt-16 grid w-full max-w-4xl grid-cols-2 gap-px overflow-hidden rounded-xl border border-fd-border bg-fd-border text-left md:grid-cols-4">
          {FACTS.map(([head, note]) => (
            <div key={head} className="bg-fd-background/80 px-4 py-4 backdrop-blur">
              <dt className="text-sm font-semibold tracking-tight text-fd-foreground">{head}</dt>
              <dd className="mt-0.5 text-xs text-fd-muted-foreground">{note}</dd>
            </div>
          ))}
        </dl>
      </div>
    </section>
  );
}

function StopRebuilding() {
  return (
    <Band>
      <SectionHeading
        eyebrow="The problem"
        title="Stop rebuilding the same agent plumbing"
        aside={
          <>
            Every line on the right is a real event type or function. Follow any row into the guide that
            documents it.
          </>
        }
      >
        Sandboxes, approvals, resume-after-crash, audit and evals get built again on every agent project, each
        one its own little system. threads writes one append-only log instead and reads all of them back out
        of it.
      </SectionHeading>
      <Ledger />
    </Band>
  );
}

function TheIdea() {
  return (
    <Band tone="sunken" pad="loose">
      <div className="flex flex-col items-center text-center">
        <Eyebrow>The idea</Eyebrow>
        <p className="mt-6 font-mono text-3xl tracking-tight text-fd-foreground sm:text-5xl">
          state = <span className="text-fd-primary">reduce</span>(log)
        </p>
        <p className="mt-6 max-w-2xl text-lg leading-8 text-pretty text-fd-muted-foreground">
          Every run is an append-only log of what the agent saw and did. Nothing is kept beside it, so nothing
          can drift from it.
        </p>
      </div>
      <LogDiagram />
    </Band>
  );
}

function TheRecord() {
  return (
    <Band id="timeline">
      <SectionHeading
        step="01"
        eyebrow="The record"
        title="Every run reads back, step by step"
        aside={
          <>
            From the command line the same run is <Code>threads timeline &lt;thread_id&gt;</Code>. Pick any
            entry below to see the stored event.
          </>
        }
      >
        There is nothing to instrument. A thread already holds every input, the exact bytes of every model
        request, every tool call, every approval and every result. <Code>timeline()</Code> reads it back in
        order, from production, months later, with only the store and the thread id.
      </SectionHeading>
      <Timeline />
      <p className="mt-6 max-w-3xl text-sm leading-6 text-fd-muted-foreground">
        This is one recorded run of a support agent on <Code>claude-sonnet-5</Code>, not a drawing of one. It
        parked waiting for a person to approve a refund, and the epoch in the margin is the lease changing
        hands: whether the approval takes a millisecond or a week, the log reads the same.
      </p>
    </Band>
  );
}

function Observability() {
  return (
    <Band id="observability" tone="card">
      <SectionHeading
        step="02"
        eyebrow="Observability"
        title="The same log, as OpenTelemetry spans"
        aside={
          <>
            The panel on the right is the recorded run above put through the rules in{" "}
            <Code>spec/otel/README.md</Code>: the turn span closes where the run parks, the resumed turn opens
            on the approval, and the span ids are derived from the event ids.
          </>
        }
      >
        One line on your host sends every turn, model call and tool call to the tracing tool you already run.
        Spans are computed from the log rather than recorded beside it, so a run that crashed and was
        recovered still traces, and an approval a person gave hours later lands in the same trace as the turn
        that asked for it.
      </SectionHeading>
      <div className="mt-12 grid items-start gap-6 lg:grid-cols-[minmax(0,0.85fr)_minmax(0,1.3fr)] lg:gap-12">
        <div className="flex min-w-0 flex-col gap-6">
          <div className={`overflow-hidden bg-fd-background ${panel}`}>
            <CodeSample sample={TELEMETRY} />
          </div>
          <div>
            <p className="text-sm leading-6 text-fd-muted-foreground">
              The exporter speaks OTLP/HTTP JSON and the standard <Code>OTEL_*</Code> variables, so the
              endpoint is the only thing that changes:
            </p>
            <div className="mt-4">
              <Backends />
            </div>
            <dl className="mt-8 space-y-4 text-sm leading-6">
              <div>
                <dt className="font-medium text-fd-foreground">Content is off by default</dt>
                <dd className="text-fd-muted-foreground">
                  Names, timings, token counts and outcomes leave the process. The conversation does not, and
                  prompts are never exported.
                </dd>
              </div>
              <div>
                <dt className="font-medium text-fd-foreground">Sent once, when a span closes</dt>
                <dd className="text-fd-muted-foreground">
                  A slow collector never holds up a run, and nothing is dropped while it is down.
                </dd>
              </div>
              <div>
                <dt className="font-medium text-fd-foreground">Nothing extra is recorded</dt>
                <dd className="text-fd-muted-foreground">
                  A pure function turns committed events into spans, so a run that crashed and was recovered
                  traces exactly like one that did not.
                </dd>
              </div>
            </dl>
          </div>
        </div>
        <Trace />
      </div>
    </Band>
  );
}

function EvalsSection() {
  return (
    <Band id="evals">
      <SectionHeading
        step="03"
        eyebrow="Evals"
        title="Save a real run. Rerun it forever."
        aside={
          <>
            A saved case reruns with zero model calls, and an effectful tool call can only answer from the
            recording, so an eval never performs a real side effect.
          </>
        }
      >
        <Code>saveCase</Code> keeps a turn you liked as a regression case: the recorded replies, the tool
        results and the exact request bytes. <Code>threads eval</Code> then checks it on every commit.
      </SectionHeading>
      <Evals />
    </Band>
  );
}

function BuiltFor() {
  return (
    <Band tone="card">
      <SectionHeading
        eyebrow="Use cases"
        title="Built for the agent you need"
        aside="Nothing above changes when you switch: the run is the same log whether it started from a script, a Slack message or a cron trigger."
      >
        The same agent definition runs as a script, in a sandbox, in Slack or on a schedule. Pick one to see
        the whole program.
      </SectionHeading>
      <UseCases />
    </Band>
  );
}

function WhatYouCanBuild() {
  return (
    <Band>
      <SectionHeading
        eyebrow="Built in"
        title="What you can build"
        aside="Every piece has the same shape in TypeScript and Python, and the public names are mapped one to one in spec/api.json."
      >
        Each piece is one option on your agent or host. Bring your API keys; threads does the plumbing.
      </SectionHeading>
      <Features />
    </Band>
  );
}

function CallToAction() {
  return (
    <section className="relative overflow-hidden bg-[var(--surface-sunken)]">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-48 left-1/2 h-96 w-[64rem] max-w-[160vw] -translate-x-1/2 rounded-full bg-fd-primary/15 blur-3xl"
      />
      <div className="relative mx-auto w-full max-w-[80rem] px-4 py-24 text-center sm:px-6 lg:py-32">
        <h2 className="text-4xl font-semibold tracking-[-0.03em] text-balance sm:text-5xl">
          Run your first agent with no API key
        </h2>
        <p className="mx-auto mt-6 max-w-xl text-lg leading-8 text-fd-muted-foreground">
          The quickstart builds a weather agent on a scripted model, so it runs offline with no keys. Then you
          swap in a real one and read back the thread you just produced.
        </p>
        <div className="mt-9 flex flex-wrap justify-center gap-3">
          <Link
            href="/docs/quickstart"
            className={`${buttonBase} h-11 bg-fd-primary px-6 text-fd-primary-foreground shadow-[var(--shadow-panel)] hover:bg-fd-primary/90`}
          >
            Read the quickstart
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
          <Link
            href="/docs/how-it-works"
            className={`${buttonBase} h-11 border border-fd-border bg-fd-background px-5 hover:bg-fd-accent hover:text-fd-accent-foreground`}
          >
            How it works
          </Link>
        </div>
      </div>
    </section>
  );
}
