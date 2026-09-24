/** A broken semantic rule (spec/schema/README.md, "Semantic rules"); undefined means none. */
export type Violation =
  | {
      readonly code:
        | "invalid_transition"
        | "approval_mismatch"
        | "unsupported_critical_event";
      readonly message: string;
    }
  | undefined;

export function invalid(message: string): Violation {
  return { code: "invalid_transition", message };
}
