import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { CodeSample } from "@/components/home/code-sample";
import { CopyCommand } from "@/components/home/copy-command";
import { Evals } from "@/components/home/evals";
import { EventLog } from "@/components/home/event-log";
import { Features } from "@/components/home/features";
import { GitHubIcon } from "@/components/home/github-icon";
import { HeroThreads } from "@/components/home/hero-threads";
import { Plumbing } from "@/components/home/plumbing";
import { Providers } from "@/components/home/providers";
import { QUICKSTART, TELEMETRY } from "@/components/home/samples";
import { Band, Code, SectionHeading } from "@/components/home/section";
import { Timeline } from "@/components/home/timeline";
import { Backends, Trace } from "@/components/home/trace";
import { UseCases } from "@/components/home/use-cases";
import { LogoMark } from "@/components/logo";
import { githubUrl, tagline } from "@/lib/shared";

const buttonBase =
  "inline-flex h-10 items-center justify-center gap-2 rounded-full px-5 text-sm font-medium transition-colors";

/*
 * The argument, in order: here it is → here is the plumbing you stop writing → here is one run,
 * read three ways (the log, the trace, the eval) → here is what you build with it → start.
 *
 * Sections alternate surfaces so two large ones never share a background, and the three "read it
 * back" sections share a numbered eyebrow so they read as one movement instead of three clones.
 */
export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col">
      <Hero />
      <Providers />
      <StopRebuilding />
      <TheRecord />
      <Observability />
      <EvalsSection />
      <BuiltFor />
      <WhatYouCanBuild />
      <CallToAction />
    </main>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-fd-border">
      <HeroThreads />
      <div className="relative mx-auto flex max-w-6xl flex-col items-center px-4 pt-14 pb-16 text-center sm:px-6 sm:pt-20 lg:pb-20">
        <LogoMark className="mb-5 h-11 w-auto text-fd-primary sm:h-12" />
        <Link
          href={githubUrl}
          className="mb-6 inline-flex items-center gap-2 rounded-full border border-fd-border bg-fd-background/70 px-3 py-1 text-xs font-medium text-fd-muted-foreground backdrop-blur transition-colors hover:text-fd-foreground"
        >
          <span className="size-1.5 rounded-full bg-fd-primary" aria-hidden="true" />
          Alpha · Open source · Apache-2.0
        </Link>
        <h1 className="max-w-4xl text-4xl leading-[1.08] font-semibold tracking-tight text-balance sm:text-5xl lg:text-6xl">
          Agents you can <span className="text-fd-primary">inspect</span>,{" "}
          <span className="text-fd-primary">replay</span> and <span className="text-fd-primary">trust</span>.
        </h1>
        <p className="mt-5 max-w-2xl text-base leading-7 text-pretty text-fd-muted-foreground sm:text-lg">
          Build in TypeScript or Python on an append-only event log. Inspect model calls, tool effects and
          approvals; use built-in sandboxes, channels, memory and evals.
        </p>
        <div className="mt-7 flex flex-wrap items-center justify-center gap-3">
          <Link
            href="/docs/quickstart"
            className={`${buttonBase} bg-fd-primary text-fd-primary-foreground hover:bg-fd-primary/90`}
          >
            Get started
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
          <Link
            href={githubUrl}
            className={`${buttonBase} border border-fd-border bg-fd-background hover:bg-fd-accent hover:text-fd-accent-foreground`}
          >
            <GitHubIcon className="size-4" />
            GitHub
          </Link>
        </div>
        <div className="mt-6 flex w-full max-w-full flex-col items-center gap-2">
          <CopyCommand command="git clone https://github.com/useagenthq/threads" />
          <p className="text-xs text-fd-muted-foreground">
            Not on npm or PyPI yet.{" "}
            <Link href="/docs/installation" className="underline underline-offset-4 hover:text-fd-foreground">
              Install from source
            </Link>
            .
          </p>
        </div>

        <div className="mt-12 grid w-full overflow-hidden rounded-2xl border border-fd-border bg-fd-card text-left shadow-2xl shadow-fd-primary/5 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0 border-fd-border lg:border-r">
            <CodeSample sample={QUICKSTART} />
          </div>
          <div className="border-t border-fd-border lg:border-t-0">
            <EventLog />
          </div>
        </div>
        <p className="mt-4 max-w-xl text-sm text-fd-muted-foreground">
          That is the whole program. Beside it are the events <Code>thread.timeline()</Code> gives back for
          that run, in order, and nothing was added to get them.
        </p>
      </div>
    </section>
  );
}

function StopRebuilding() {
  return (
    <Band tone="card">
      <SectionHeading
        eyebrow="The problem"
        title="Stop rebuilding the same agent plumbing"
        aside={
          <>
            The v1 schema has 73 event types, each stored as one canonical JSON line. Those exact bytes are
            what SQLite holds, what <Code>threads export</Code> writes and what the conformance fixtures pin,
            so a log written by the TypeScript library reduces to the same state in Python.
          </>
        }
      >
        Sandboxes, approvals, resume-after-crash, audit and evals get built again on every agent project, each
        one its own little system. threads writes one append-only log instead and reads all of them back out
        of it.
      </SectionHeading>
      <Plumbing />
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
      <div className="mt-12 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] lg:gap-10">
        <div className="flex min-w-0 flex-col gap-6">
          <div className="overflow-hidden rounded-2xl border border-fd-border bg-fd-background">
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
    <section className="relative overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 left-1/2 h-80 w-[56rem] max-w-[140vw] -translate-x-1/2 rounded-full bg-fd-primary/12 blur-3xl"
      />
      <div className="relative mx-auto w-full max-w-6xl px-4 py-20 text-center sm:px-6 lg:py-28">
        <p className="font-mono text-sm font-medium tracking-wide text-fd-primary">state = reduce(log)</p>
        <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl lg:text-5xl">
          Run your first agent with no API key
        </h2>
        <p className="mx-auto mt-5 max-w-xl text-base leading-7 text-fd-muted-foreground">
          The quickstart builds a weather agent on a scripted model, so it runs offline with no keys. Then you
          swap in a real one and read back the thread you just produced.
        </p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Link
            href="/docs/quickstart"
            className={`${buttonBase} bg-fd-primary text-fd-primary-foreground hover:bg-fd-primary/90`}
          >
            Read the quickstart
            <ArrowRight className="size-4" aria-hidden="true" />
          </Link>
          <Link
            href="/docs/how-it-works"
            className={`${buttonBase} border border-fd-border bg-fd-background hover:bg-fd-accent hover:text-fd-accent-foreground`}
          >
            How it works
          </Link>
        </div>
      </div>
    </section>
  );
}
