import Link from "next/link";
import { Logo } from "@/components/logo";
import { githubUrl, tagline } from "@/lib/shared";

const COLUMNS = [
  {
    title: "Docs",
    links: [
      { text: "Quickstart", href: "/docs/quickstart" },
      { text: "Installation", href: "/docs/installation" },
      { text: "How it works", href: "/docs/how-it-works" },
      { text: "Comparison", href: "/docs/comparison" },
    ],
  },
  {
    title: "Reference",
    links: [
      { text: "API reference", href: "/docs/reference/overview" },
      { text: "HTTP API", href: "/docs/http-api" },
      { text: "CLI", href: "/docs/production/cli" },
      { text: "llms.txt", href: "/llms.txt" },
    ],
  },
  {
    title: "Project",
    links: [
      { text: "GitHub", href: githubUrl },
      { text: "Issues", href: `${githubUrl}/issues` },
      { text: "License", href: `${githubUrl}/blob/main/LICENSE` },
    ],
  },
];

export function Footer() {
  return (
    <footer className="border-t border-fd-border">
      <div className="mx-auto grid w-full max-w-6xl gap-10 px-4 py-12 sm:px-6 md:grid-cols-[1.5fr_repeat(3,1fr)]">
        <div className="flex flex-col gap-3">
          <Logo />
          <p className="max-w-xs text-sm text-fd-muted-foreground">{tagline}</p>
        </div>
        {COLUMNS.map((c) => (
          <nav key={c.title} aria-label={c.title} className="flex flex-col gap-2 text-sm">
            <p className="font-medium">{c.title}</p>
            {c.links.map((l) => (
              <Link
                key={l.href}
                href={l.href}
                className="w-fit text-fd-muted-foreground transition-colors hover:text-fd-foreground"
              >
                {l.text}
              </Link>
            ))}
          </nav>
        ))}
      </div>
      <div className="border-t border-fd-border">
        <p className="mx-auto w-full max-w-6xl px-4 py-6 text-xs text-fd-muted-foreground sm:px-6">
          Threads AI is open source under the Apache-2.0 license.
        </p>
      </div>
    </footer>
  );
}
