import {
  CodeBlockTab,
  CodeBlockTabs,
  CodeBlockTabsList,
  CodeBlockTabsTrigger,
} from "fumadocs-ui/components/codeblock";
import { ServerCodeBlock } from "fumadocs-ui/components/codeblock.rsc";
import type { Sample } from "./samples";

const LANGS = [
  // Values match fumadocs' escaped <Tabs items> ("typescript"), so the persisted docs choice selects a tab here.
  { value: "typescript", label: "TypeScript", lang: "ts", key: "ts" },
  { value: "python", label: "Python", lang: "python", key: "py" },
] as const;

/** TS/Python sample. Shares the "lang" tab group with the docs, so the choice carries over. */
export function CodeSample({ sample }: { sample: Sample }) {
  return (
    <CodeBlockTabs
      groupId="lang"
      persist
      defaultValue="typescript"
      className="my-0 h-full rounded-none border-0 bg-transparent"
    >
      <CodeBlockTabsList>
        {LANGS.map((l) => (
          <CodeBlockTabsTrigger key={l.value} value={l.value}>
            {l.label}
          </CodeBlockTabsTrigger>
        ))}
      </CodeBlockTabsList>
      {LANGS.map((l) => (
        <CodeBlockTab key={l.value} value={l.value}>
          <ServerCodeBlock
            code={sample[l.key]}
            lang={l.lang}
            codeblock={{ className: "my-0 rounded-none border-0" }}
          />
        </CodeBlockTab>
      ))}
    </CodeBlockTabs>
  );
}
