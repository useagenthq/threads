import Link from "next/link";

// Only providers with a shipped model adapter (typescript/packages, python/src/threads). Keep in step with
// the Models guide.
const PROVIDERS = [
  { name: "Anthropic", note: "TypeScript · Python" },
  { name: "OpenAI", note: "TypeScript · Python" },
  { name: "AI SDK", note: "Any AI SDK provider · TypeScript" },
  { name: "LiteLLM", note: "OpenAI-compatible endpoints · Python" },
] as const;

export function Providers() {
  return (
    <section aria-labelledby="providers-heading" className="border-b border-fd-border">
      <div className="mx-auto flex w-full max-w-6xl flex-col items-center gap-5 px-4 py-7 sm:px-6 lg:flex-row lg:gap-10">
        <h2
          id="providers-heading"
          className="shrink-0 font-mono text-[0.68rem] tracking-widest text-fd-muted-foreground uppercase"
        >
          Works with the models you use
        </h2>
        <ul className="grid w-full grid-cols-2 gap-x-6 gap-y-4 sm:grid-cols-4 lg:flex-1">
          {PROVIDERS.map((p) => (
            <li
              key={p.name}
              className="flex flex-col items-center gap-0.5 text-center lg:items-start lg:text-left"
            >
              <span className="text-base font-semibold tracking-tight text-fd-foreground">{p.name}</span>
              <span className="text-xs leading-4 text-fd-muted-foreground">{p.note}</span>
            </li>
          ))}
        </ul>
        <Link
          href="/docs/agents/models"
          className="shrink-0 text-sm text-fd-muted-foreground underline underline-offset-4 hover:text-fd-foreground"
        >
          Models, retries and fallbacks
        </Link>
      </div>
    </section>
  );
}
