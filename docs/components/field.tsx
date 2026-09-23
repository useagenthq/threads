import type { ReactNode } from "react";

type FieldProps = {
  name: string;
  type?: string;
  required?: boolean;
  default?: string;
  children?: ReactNode;
};

/** One parameter or field: name, type and flags on one line, prose below. */
export function Field({ name, type, required, default: fallback, children }: FieldProps) {
  return (
    <div className="not-prose border-b border-fd-border py-4 first:pt-1 last:border-b-0">
      <div className="flex flex-wrap items-baseline gap-x-2.5 gap-y-1 text-sm">
        <code className="font-mono font-semibold text-fd-foreground">{name}</code>
        {type ? (
          <code className="font-mono text-[0.8125rem] break-all text-fd-muted-foreground">{type}</code>
        ) : null}
        {required ? (
          <span className="rounded-md bg-fd-primary/10 px-1.5 py-0.5 text-xs font-medium text-fd-primary">
            required
          </span>
        ) : null}
        {fallback ? (
          <span className="text-xs text-fd-muted-foreground">
            default <code className="font-mono text-fd-foreground">{fallback}</code>
          </span>
        ) : null}
      </div>
      {children ? (
        <div className="prose prose-no-margin mt-2 text-sm text-fd-muted-foreground [&_p]:my-0">
          {children}
        </div>
      ) : null}
    </div>
  );
}
