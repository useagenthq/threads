import type { SVGProps } from "react";

/** The mark: three threads running into their ends. Drawn in currentColor, so the parent sets its color. */
export function LogoMark(props: SVGProps<SVGSVGElement>) {
  return (
    <svg viewBox="0 0 1000 880" aria-hidden="true" {...props}>
      <g fill="none" stroke="currentColor" strokeWidth="72" strokeLinecap="round" strokeLinejoin="round">
        <path d="M110 280 H420 C480 280 500 250 500 200 V155 C500 105 530 80 585 80 H790" />
        <path d="M110 440 H515 C570 440 590 410 590 360 V315 C590 265 620 240 675 240 H875" />
        <path d="M110 600 H485 C540 600 565 630 565 680 V715 C565 765 595 785 650 785 H830" />
      </g>
      <g fill="currentColor">
        <circle cx="820" cy="80" r="58" />
        <circle cx="905" cy="240" r="58" />
        <circle cx="860" cy="785" r="58" />
      </g>
    </svg>
  );
}

export function Logo() {
  return (
    <span className="inline-flex items-center gap-2">
      <LogoMark className="h-5.5 w-auto text-fd-primary" />
      <span className="text-[0.95rem] font-semibold tracking-tight">
        Threads <span className="text-fd-muted-foreground">AI</span>
      </span>
    </span>
  );
}
