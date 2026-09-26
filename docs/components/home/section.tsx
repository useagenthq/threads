import type { ReactNode } from "react";

/*
 * The shapes every landing section is built from.
 *
 * Two of them carry the page's rhythm. `Band` alternates the surface under a section so two big
 * sections never sit on the same colour in a row, and `SectionHeading` puts a second column beside
 * the title: at 1440 a lone max-w-2xl heading leaves half the row empty, which is most of what
 * "it feels empty" was. `step` numbers the three sections that read the same log three ways.
 */

export function Code({ children }: { children: ReactNode }) {
  return (
    <code className="rounded border border-fd-border bg-fd-muted px-1 py-0.5 font-mono text-[0.85em] text-fd-foreground">
      {children}
    </code>
  );
}

export function Band({
  id,
  tone = "plain",
  children,
}: {
  id?: string;
  tone?: "plain" | "card";
  children: ReactNode;
}) {
  return (
    <section id={id} className={`border-b border-fd-border ${tone === "card" ? "bg-fd-card/60" : ""}`}>
      <div className="mx-auto w-full max-w-6xl px-4 py-16 sm:px-6 lg:py-24">{children}</div>
    </section>
  );
}

export function SectionHeading({
  step,
  eyebrow,
  title,
  aside,
  children,
}: {
  step?: string;
  eyebrow: string;
  title: string;
  aside?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="grid gap-x-12 gap-y-6 lg:grid-cols-[minmax(0,1.3fr)_minmax(0,1fr)]">
      <div className="max-w-2xl">
        <p className="flex items-center gap-2.5 font-mono text-xs font-medium tracking-widest uppercase">
          {step ? (
            <span className="rounded border border-fd-primary/30 bg-fd-primary/10 px-1.5 py-0.5 text-fd-primary tabular-nums">
              {step}
            </span>
          ) : null}
          <span className="text-fd-primary">{eyebrow}</span>
        </p>
        <h2 className="mt-4 text-3xl font-semibold tracking-tight text-balance sm:text-4xl">{title}</h2>
        <p className="mt-4 text-base leading-7 text-fd-muted-foreground">{children}</p>
      </div>
      {aside ? (
        <p className="text-sm leading-6 text-fd-muted-foreground lg:mt-1 lg:border-l lg:border-fd-border lg:pl-12">
          {aside}
        </p>
      ) : null}
    </div>
  );
}
