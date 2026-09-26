import { Tabs, TabsContent, TabsList, TabsTrigger } from "fumadocs-ui/components/ui/tabs";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import { CodeSample } from "./code-sample";
import { USE_CASES } from "./samples";
import { panel } from "./section";

/** One tab per use case; each code sample follows the shared TS/Python choice. */
export function UseCases() {
  return (
    <Tabs defaultValue={USE_CASES[0]?.id} className="mt-12">
      <TabsList aria-label="Use cases" className="grid grid-cols-2 gap-2 lg:grid-cols-5">
        {USE_CASES.map((u) => (
          <TabsTrigger
            key={u.id}
            value={u.id}
            className="rounded-lg border border-fd-border bg-fd-background px-4 py-3 text-left text-sm font-medium text-fd-muted-foreground transition-colors hover:border-fd-primary/40 hover:text-fd-foreground data-[active]:border-fd-primary data-[active]:bg-fd-primary data-[active]:text-fd-primary-foreground"
          >
            {u.title}
          </TabsTrigger>
        ))}
      </TabsList>
      {USE_CASES.map((u) => (
        <TabsContent key={u.id} value={u.id} className={`mt-4 overflow-hidden bg-fd-background ${panel}`}>
          <div className="flex flex-col gap-2 border-b border-fd-border px-5 py-4 sm:flex-row sm:items-center sm:justify-between">
            <p className="text-sm leading-6 text-fd-muted-foreground">{u.body}</p>
            <Link
              href={u.href}
              className="inline-flex shrink-0 items-center gap-1 text-sm font-medium text-fd-primary hover:underline"
            >
              Read the guide
              <ArrowRight className="size-3.5" aria-hidden="true" />
            </Link>
          </div>
          <CodeSample sample={u} />
        </TabsContent>
      ))}
    </Tabs>
  );
}
