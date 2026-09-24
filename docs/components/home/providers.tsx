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
      <div className="mx-auto flex w-full max-w-6xl flex-col items-center gap-6 px-4 py-10 sm:px-6">
        <h2 id="providers-heading" className="text-sm font-medium text-fd-muted-foreground">
          Works with the models you use
        </h2>
        <ul className="grid w-full grid-cols-2 gap-x-6 gap-y-6 md:grid-cols-4">
          {PROVIDERS.map((p) => (
            <li key={p.name} className="flex flex-col items-center gap-1 text-center">
              <span className="text-xl font-semibold tracking-tight text-fd-foreground/80">{p.name}</span>
              <span className="text-xs text-fd-muted-foreground">{p.note}</span>
            </li>
          ))}
        </ul>
        <Link
          href="/docs/agents/models"
          className="text-sm text-fd-muted-foreground underline underline-offset-4 hover:text-fd-foreground"
        >
          Models, retries and fallbacks
        </Link>
      </div>
    </section>
  );
}
