import type { ReactNode } from "react";

/*
 * The shapes every landing section is built from.
 *
 * Three jobs here, all of them about silhouette. `Band` sets the surface and the width: panels run
 * to 80rem while prose is capped at 34rem inside the heading, so the two never share an outline —
 * that width contrast is what the page was missing. `tone` alternates the surface between the page
 * colour, a raised card and a sunken well, so depth does the separating that borders alone could
 * not. `SectionHeading` puts a second column beside the title, because a lone 34rem heading on a
 * 1440 page leaves half the row empty.
 *
 * `step` numbers the three sections that read the same log three ways, in a solid brand chip: it is
 * the accent that keeps recurring below the fold.
 */

const SURFACE = {
  plain: "",
  card: "bg-fd-card/70",
  sunken: "bg-[var(--surface-sunken)]",
} as const;

/** A raised panel: hairline border, 16px radius, one soft shadow step. Small parts stay tighter. */
export const panel = "rounded-2xl border border-fd-border shadow-[var(--shadow-panel)]";

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
  pad = "normal",
  children,
}: {
  id?: string;
  tone?: keyof typeof SURFACE;
  pad?: "normal" | "tight" | "loose";
  children: ReactNode;
}) {
  const y = pad === "tight" ? "py-12 lg:py-16" : pad === "loose" ? "py-20 lg:py-32" : "py-14 lg:py-20";
  return (
    <section id={id} className={`border-b border-fd-border ${SURFACE[tone]}`}>
      <div className={`mx-auto w-full max-w-[80rem] px-4 sm:px-6 ${y}`}>{children}</div>
    </section>
  );
}

export function Eyebrow({ step, children }: { step?: string; children: string }) {
  return (
    <p className="flex items-center gap-3 font-mono text-[0.8rem] font-medium tracking-[0.18em] uppercase">
      {step ? (
        <span className="rounded bg-fd-primary px-1.5 py-0.5 text-fd-primary-foreground tabular-nums">
          {step}
        </span>
      ) : (
        <span className="h-3 w-1 rounded-full bg-fd-primary" aria-hidden="true" />
      )}
      <span className="text-fd-foreground">{children}</span>
    </p>
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
  children?: ReactNode;
}) {
  return (
    <div className="grid gap-x-16 gap-y-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,22rem)]">
      <div className="max-w-[34rem]">
        <Eyebrow step={step}>{eyebrow}</Eyebrow>
        <h2 className="mt-5 text-[2rem] leading-[1.1] font-semibold tracking-tight text-balance sm:text-[2.6rem]">
          {title}
        </h2>
        {children ? <p className="mt-5 text-base leading-7 text-fd-muted-foreground">{children}</p> : null}
      </div>
      {aside ? (
        <p className="self-end border-l-2 border-fd-primary/30 pl-5 text-sm leading-6 text-fd-muted-foreground">
          {aside}
        </p>
      ) : null}
    </div>
  );
}
