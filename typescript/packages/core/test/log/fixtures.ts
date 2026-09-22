import {
  canonicalize,
  type ParsedLine,
  parseLogLine,
  parseStrictJson,
} from "../../src/log";

export const HASH: string = "a".repeat(64);
export const alice = { issuer: "api", tenant: "acme", subject: "alice" };

export function envelope(
  type: string,
  data: unknown,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    seq: 2,
    event_id: "0192e000-0000-7000-8000-000000000002",
    thread_id: "0192a000-0000-7000-8000-000000000001",
    branch_id: "0192b000-0000-7000-8000-000000000001",
    epoch: 1,
    type,
    type_version: 1,
    time: 1790000002000,
    actor: { kind: "host" },
    prev_hash: HASH,
    critical: true,
    data,
    ...extra,
  };
}

export function parse(value: unknown): ReturnType<typeof parseLogLine> {
  return parseLogLine(jcs(value));
}

export function kind(value: unknown): ParsedLine["kind"] | string {
  const result = parse(value);
  return result.ok ? result.value.kind : result.error.code;
}

export const header = {
  format: "threads.log",
  format_version: 1,
  thread_id: "0192a000-0000-7000-8000-000000000001",
  branch_id: "0192b000-0000-7000-8000-000000000001",
  created_at: 1790000000000,
  writer: { impl: "threads-ts", version: "0.1.0" },
};

/** The canonical line for a test value; tests build values, lines must be JCS. */
export function jcs(value: unknown): string {
  const json = parseStrictJson(JSON.stringify(value));
  const text = json.ok ? canonicalize(json.value) : json;
  if (!text.ok) throw new Error("test value is not JSON");
  return text.value;
}
