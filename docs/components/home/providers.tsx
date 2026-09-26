import Link from "next/link";
import { panel } from "./section";

// Only providers with a shipped model adapter (typescript/packages, python/src/threads). Keep in step with
// the Models guide. There are no logos here on purpose: we do not have permission to use anyone's mark.
const PROVIDERS = [
  { name: "Anthropic", note: "TypeScript · Python" },
  { name: "OpenAI", note: "TypeScript · Python" },
  { name: "AI SDK", note: "Any AI SDK provider · TypeScript" },
  { name: "LiteLLM", note: "OpenAI-compatible endpoints · Python" },
] as const;

/** A bordered, divided rail rather than four labels floating in a band. */
export function Providers() {
  return (
    <section aria-labelledby="providers-heading" className="border-b border-fd-border bg-fd-card/70">
      <div className="mx-auto w-full max-w-[80rem] px-4 py-10 sm:px-6">
        <div className={`overflow-hidden bg-fd-background ${panel}`}>
          <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-1 border-b border-fd-border px-5 py-2.5">
            <h2
              id="providers-heading"
              className="font-mono text-[0.72rem] tracking-[0.16em] text-fd-foreground uppercase"
            >
              Works with the models you use
            </h2>
            <Link
              href="/docs/agents/models"
              className="font-mono text-[0.72rem] text-fd-primary hover:underline"
            >
              Models, retries and fallbacks →
            </Link>
          </div>
          <ul className="grid grid-cols-2 divide-fd-border sm:grid-cols-4 sm:divide-x">
            {PROVIDERS.map((p) => (
              <li
                key={p.name}
                className="border-fd-border px-5 py-4 [&:nth-child(-n+2)]:border-b sm:[&:nth-child(-n+2)]:border-b-0"
              >
                <p className="text-lg font-semibold tracking-tight text-fd-foreground">{p.name}</p>
                <p className="mt-0.5 text-xs leading-5 text-fd-muted-foreground">{p.note}</p>
              </li>
            ))}
          </ul>
        </div>
      </div>
    </section>
  );
}
