"use client";

import { Tabs, TabsList, TabsTrigger } from "fumadocs-ui/components/ui/tabs";

// Values are fumadocs' escaped <Tabs items> ("TypeScript" -> "typescript"), the ones every lang tab stores.
const LANGS = [
  { value: "typescript", short: "TS", label: "TypeScript" },
  { value: "python", short: "Py", label: "Python" },
] as const;

/**
 * The site-wide language switch. It is a tab list in the shared "lang" group with no panels, so picking a
 * language flips every lang tab on the page and persists the choice, exactly like clicking one of them.
 */
export function LangSwitch() {
  return (
    <Tabs groupId="lang" persist defaultValue="typescript" className="ms-3">
      <TabsList
        aria-label="Code language"
        className="flex rounded-full border border-fd-border bg-fd-muted p-0.5 text-xs font-medium"
      >
        {LANGS.map((l) => (
          <TabsTrigger
            key={l.value}
            value={l.value}
            className="rounded-full px-2.5 py-1 text-fd-muted-foreground transition-colors hover:text-fd-foreground data-[active]:bg-fd-background data-[active]:text-fd-primary data-[active]:shadow-sm"
          >
            <span className="sm:hidden">{l.short}</span>
            <span className="max-sm:hidden">{l.label}</span>
          </TabsTrigger>
        ))}
      </TabsList>
    </Tabs>
  );
}
