import type { SVGProps } from "react";

/** The mark: log lines of an append-only record, with a thread running through them. */
export function LogoMark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 32 32" fill="none" aria-hidden="true" {...props}>
      <rect width="32" height="32" rx="8" fill="#3452D1" />
      <path d="M8 10h16M8 16h12M8 22h8" stroke="#fff" strokeWidth="2.4" strokeLinecap="round" />
      <path d="M25 6c-6 4 2 10-4 14s-2 7 2 7" stroke="#8EA2F8" strokeWidth="2.4" strokeLinecap="round" />
    </svg>
  );
}

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2">
      <LogoMark className="size-6" />
      <span className="text-[0.95rem] font-semibold tracking-tight">
        Threads <span className="text-fd-muted-foreground">AI</span>
      </span>
    </span>
  );
}
