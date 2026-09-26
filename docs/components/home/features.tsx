import {
  ArrowRight,
  Bot,
  Box,
  Brain,
  FlaskConical,
  type LucideIcon,
  MessageCircle,
  ShieldCheck,
  SlidersHorizontal,
  SquareCode,
  Users,
} from "lucide-react";
import Link from "next/link";
import { panel } from "./section";

/** Each card is one option on agent() or host(); the tags are the names you actually type. */
type Feature = {
  readonly title: string;
  readonly icon: LucideIcon;
  readonly href: string;
  readonly tags: readonly string[];
  readonly body: string;
};

const FEATURES: readonly Feature[] = [
  {
    title: "Evals & testing",
    icon: FlaskConical,
    href: "/docs/evals/saved-cases",
    tags: ["saveCase", "Scripted model", "Fake sandbox"],
    body: "Save a real run as a regression case and replay it with a scripted model: no API keys, no network.",
  },
  {
    title: "Sandboxes",
    icon: Box,
    href: "/docs/sandboxes/overview",
    tags: ["E2B", "Daytona", "Modal · Python"],
    body: "Run code in an isolated machine with no internet by default. Your keys never enter it.",
  },
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

export function Features() {
  return (
    <ul className={`mt-12 grid gap-px overflow-hidden bg-fd-border sm:grid-cols-2 lg:grid-cols-3 ${panel}`}>
      {FEATURES.map((f) => (
        <li key={f.title} className="bg-fd-background">
          <Link
            href={f.href}
            className="group flex h-full flex-col gap-3 p-6 transition-colors hover:bg-fd-card focus-visible:-outline-offset-2"
          >
            <span className="flex items-center gap-3">
              <span className="grid size-9 place-items-center rounded-lg border border-fd-border bg-fd-card text-fd-primary transition-colors group-hover:border-fd-primary/40 group-hover:bg-fd-primary/10">
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
  );
}
