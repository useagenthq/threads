import {
  ArrowRight,
  Bot,
  Box,
  Brain,
  FlaskConical,
  GitFork,
  type LucideIcon,
  MessageCircle,
  ScrollText,
  Shield,
  ShieldCheck,
  SlidersHorizontal,
  SquareCode,
  Users,
} from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { CodeSample } from "@/components/home/code-sample";
import { CopyCommand } from "@/components/home/copy-command";
import { EventLog } from "@/components/home/event-log";
import { GitHubIcon } from "@/components/home/github-icon";
import { LogoMark } from "@/components/logo";
import { githubUrl, tagline } from "@/lib/shared";

type Feature = { title: string; icon: LucideIcon; href: string; tags: string[]; body: string };

const FEATURES: Feature[] = [
  {
    title: "Agents & tools",
    icon: Bot,
    href: "/docs/agents/agents",
    tags: ["agent()", "tool()", "MCP"],
    body: "A model, instructions and tools. Built-in shell, file, web, git and code tools, plus any MCP server.",
  },
  {
    title: "Multi-agent",
    icon: Users,
    href: "/docs/multi-agent/subagents",
    tags: ["Subagents", "Handoffs", "Teams"],
    body: "Let an agent start helpers, hand the conversation to a specialist, or share a task board.",
  },
  {
    title: "Sandboxes",
    icon: Box,
    href: "/docs/sandboxes/overview",
    tags: ["E2B", "Daytona", "Modal · Python"],
    body: "Run code in an isolated machine with no internet by default. Your keys never enter it.",
  },
  {
    title: "Channels",
    icon: MessageCircle,
    href: "/docs/host/overview",
    tags: ["Slack", "WhatsApp", "GitHub", "HTTP"],
    body: "Put an agent in Slack, WhatsApp or GitHub, on a cron schedule, or behind an HTTP API.",
  },
  {
    title: "Memory & knowledge",
    icon: Brain,
    href: "/docs/memory/memory",
    tags: ["Local", "Supermemory", "Zep"],
    body: "Remember across runs and search your own documents, scoped per tenant.",
  },
  {
    title: "Durability",
    icon: ShieldCheck,
    href: "/docs/production/durability",
    tags: ["Crash-safe", "Approvals", "Budgets"],
    body: "Resume after a crash without repeating an action. Park risky steps for a human to decide.",
  },
  {
    title: "Evals & testing",
    icon: FlaskConical,
    href: "/docs/evals/timeline",
    tags: ["Timeline", "Fork", "Scripted model"],
    body: "Inspect every step, fork a past run to try a fix, and test with no real model calls.",
  },
  {
    title: "Hooks & permissions",
    icon: SlidersHorizontal,
    href: "/docs/control/hooks",
    tags: ["Hooks", "Rules", "Plan mode"],
    body: "Gate tools, inject context, and decide who can approve what.",
  },
  {
    title: "API reference",
    icon: SquareCode,
    href: "/docs/reference/overview",
    tags: ["TypeScript", "Python", "HTTP"],
    body: "Every public function and type, side by side in both languages.",
  },
];

const REASONS: { title: string; icon: LucideIcon; body: ReactNode }[] = [
  {
    title: "Auditable",
    icon: ScrollText,
    body: (
      <>
        Every input, every exact model request, every tool call and result. Read it with{" "}
        <Code>timeline()</Code> or <Code>threads timeline</Code>.
      </>
    ),
  },
  {
    title: "Crash-safe",
    icon: Shield,
    body: "After a crash, an action that may already have happened is checked or handed to you, never blindly retried.",
  },
  {
    title: "Easy evals",
    icon: GitFork,
    body: "Fork any past step into its own sandbox, test with a scripted model, and save real threads as regression cases.",
  },
];

function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded border border-fd-border bg-fd-muted px-1 py-0.5 font-mono text-[0.85em] text-fd-foreground">
      {children}
    </code>
  );
}

const buttonBase =
  "inline-flex h-10 items-center justify-center gap-2 rounded-full px-5 text-sm font-medium transition-colors";

export default function HomePage() {
  return (
    <main className="flex flex-1 flex-col">
      <Hero />
      <Features />
      <Reasons />
      <CallToAction />
    </main>
  );
}

function Hero() {
  return (
    <section className="relative overflow-hidden border-b border-fd-border">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 [background-image:linear-gradient(to_right,var(--color-fd-border)_1px,transparent_1px),linear-gradient(to_bottom,var(--color-fd-border)_1px,transparent_1px)] [background-size:48px_48px] [mask-image:radial-gradient(ellipse_70%_60%_at_50%_0%,#000_40%,transparent_100%)]"
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute -top-40 left-1/2 h-[28rem] w-[56rem] max-w-[140vw] -translate-x-1/2 rounded-full bg-fd-primary/15 blur-3xl"
      />
      <div className="relative mx-auto flex max-w-6xl flex-col items-center px-4 pt-16 pb-16 text-center sm:px-6 sm:pt-24 lg:pb-24">
        <LogoMark className="mb-6 h-12 w-auto text-fd-primary sm:h-14" />
        <Link
          href={githubUrl}
          className="mb-6 inline-flex items-center gap-2 rounded-full border border-fd-border bg-fd-background/70 px-3 py-1 text-xs font-medium text-fd-muted-foreground backdrop-blur transition-colors hover:text-fd-foreground"
        >
          <span className="size-1.5 rounded-full bg-fd-primary" aria-hidden="true" />
          Alpha · Open source · Apache-2.0
        </Link>
        <h1 className="max-w-4xl text-4xl leading-[1.08] font-semibold tracking-tight text-balance sm:text-5xl lg:text-6xl">
          An agent framework for TypeScript and Python, built on an{" "}
          <span className="text-fd-primary">append-only event log</span>.
        </h1>
        <p className="mt-6 max-w-2xl text-base leading-7 text-pretty text-fd-muted-foreground sm:text-lg">
          Sandboxes, channels, memory, hooks and evals come built in. You write what your agent does and add
          your API keys.
        </p>
        <div className="mt-8 flex flex-wrap items-center justify-center gap-3">
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
        <div className="mt-6 flex w-full justify-center">
          <CopyCommand command="git clone https://github.com/useagenthq/threads" />
        </div>
        <p className="mt-2 text-xs text-fd-muted-foreground">
          Not on npm or PyPI yet.{" "}
          <Link href="/docs/installation" className="underline underline-offset-4 hover:text-fd-foreground">
            Install from source
          </Link>
          .
        </p>

        <div className="mt-14 grid w-full overflow-hidden rounded-2xl border border-fd-border bg-fd-card text-left shadow-2xl shadow-fd-primary/5 lg:grid-cols-[minmax(0,1fr)_20rem]">
          <div className="min-w-0 border-fd-border lg:border-r">
            <CodeSample />
          </div>
          <div className="border-t border-fd-border lg:border-t-0">
            <EventLog />
          </div>
        </div>
        <p className="mt-4 max-w-xl text-sm text-fd-muted-foreground">
          Every run is a log you can read, resume and fork. The right side is what{" "}
          <Code>thread.timeline()</Code> returns for this agent.
        </p>
      </div>
    </section>
  );
}

function SectionHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <div className="max-w-2xl">
      <p className="font-mono text-xs font-medium tracking-widest text-fd-primary uppercase">{eyebrow}</p>
      <h2 className="mt-3 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">{title}</h2>
      <p className="mt-4 text-base leading-7 text-fd-muted-foreground">{children}</p>
    </div>
  );
}

function Features() {
  return (
    <section className="mx-auto w-full max-w-6xl px-4 py-20 sm:px-6 lg:py-28">
      <SectionHeading eyebrow="Built in" title="What you can build">
        Each piece is one option on your agent or host. Bring your API keys; threads does the plumbing.
      </SectionHeading>
      <ul className="mt-12 grid gap-px overflow-hidden rounded-2xl border border-fd-border bg-fd-border sm:grid-cols-2 lg:grid-cols-3">
        {FEATURES.map((f) => (
          <li key={f.title} className="bg-fd-background">
            <Link
              href={f.href}
              className="group flex h-full flex-col gap-3 p-6 transition-colors hover:bg-fd-card focus-visible:-outline-offset-2"
            >
              <span className="flex items-center gap-3">
                <span className="grid size-9 place-items-center rounded-lg border border-fd-border bg-fd-card text-fd-primary">
                  <f.icon className="size-[18px]" aria-hidden="true" />
                </span>
                <span className="font-semibold">{f.title}</span>
                <ArrowRight
                  className="ml-auto size-4 -translate-x-1 text-fd-muted-foreground opacity-0 transition group-hover:translate-x-0 group-hover:opacity-100"
                  aria-hidden="true"
                />
              </span>
              <span className="text-sm leading-6 text-fd-muted-foreground">{f.body}</span>
              <span className="mt-auto flex flex-wrap gap-1.5 pt-2">
                {f.tags.map((t) => (
                  <span
                    key={t}
                    className="rounded-md border border-fd-border px-1.5 py-0.5 font-mono text-[0.7rem] text-fd-muted-foreground"
                  >
                    {t}
                  </span>
                ))}
              </span>
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Reasons() {
  return (
    <section className="border-y border-fd-border bg-fd-card/60">
      <div className="mx-auto w-full max-w-6xl px-4 py-20 sm:px-6 lg:py-28">
        <SectionHeading eyebrow="Why threads" title="One record makes the hard parts simple">
          Every run is an append-only log of what the agent saw and did. That one record is what makes these
          work.
        </SectionHeading>
        <div className="mt-12 grid gap-10 md:grid-cols-3">
          {REASONS.map((r) => (
            <div key={r.title} className="flex flex-col gap-3 border-l-2 border-fd-primary/40 pl-5">
              <r.icon className="size-5 text-fd-primary" aria-hidden="true" />
              <h3 className="text-lg font-semibold">{r.title}</h3>
              <p className="text-sm leading-6 text-fd-muted-foreground">{r.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function CallToAction() {
  return (
    <section className="mx-auto w-full max-w-6xl px-4 py-20 sm:px-6 lg:py-28">
      <div className="relative overflow-hidden rounded-2xl border border-fd-border bg-fd-card px-6 py-12 text-center sm:px-12">
        <div
          aria-hidden="true"
          className="pointer-events-none absolute -bottom-32 left-1/2 h-64 w-[40rem] max-w-[120vw] -translate-x-1/2 rounded-full bg-fd-primary/15 blur-3xl"
        />
        <h2 className="relative text-3xl font-semibold tracking-tight text-balance sm:text-4xl">
          Run your first agent with no API key
        </h2>
        <p className="relative mx-auto mt-4 max-w-xl text-fd-muted-foreground">{tagline}</p>
        <div className="relative mt-8 flex flex-wrap justify-center gap-3">
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
