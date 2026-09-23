"use client";
import { Check, Copy } from "lucide-react";
import { useState } from "react";

export function CopyCommand({ command }: { command: string }) {
  const [copied, setCopied] = useState(false);

  async function copy() {
    await navigator.clipboard.writeText(command);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="flex max-w-full items-center gap-3 rounded-lg border border-fd-border bg-fd-card py-1.5 pr-1.5 pl-4 font-mono text-[0.8125rem] text-fd-muted-foreground">
      <span aria-hidden="true" className="select-none text-fd-primary">
        $
      </span>
      <code className="truncate text-fd-foreground">{command}</code>
      <button
        type="button"
        onClick={copy}
        aria-label={copied ? "Copied" : "Copy command"}
        className="grid size-8 shrink-0 place-items-center rounded-md transition-colors hover:bg-fd-accent hover:text-fd-accent-foreground"
      >
        {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
      </button>
    </div>
  );
}
