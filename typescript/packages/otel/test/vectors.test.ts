import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { ConfigError } from "@threads/core";
import { z } from "zod";
import { type Options, resolve } from "../src/env";
import { lossIds, nonzero, spanId, traceId } from "../src/ids";
import { OTEL } from "./goldens";

// spec/otel/vectors: the id derivation and the configuration precedence, as Python reads them
// (python/tests/otel/test_vectors.py).

const read = (name: string): unknown =>
  JSON.parse(readFileSync(join(OTEL, "vectors", name), "utf8"));

const Ids = z.object({
  spans: z.array(
    z.object({
      branch_id: z.string(),
      event_id: z.string(),
      call_id: z.string().optional(),
      span_id: z.string(),
    }),
  ),
  traces: z.array(
    z.object({
      thread_id: z.string(),
      event_id: z.string(),
      trace_id: z.string(),
    }),
  ),
  losses: z.array(
    z.object({
      observer: z.string(),
      thread_id: z.string(),
      deleted_at: z.int(),
      trace_id: z.string(),
      span_id: z.string(),
    }),
  ),
  nonzero: z.array(z.object({ digest: z.string(), id: z.string() })),
});

const Env = z.object({
  cases: z.array(
    z.object({
      name: z.string(),
      env: z.record(z.string(), z.string()),
      options: z.object({
        endpoint: z.string().optional(),
        headers: z.record(z.string(), z.string()).optional(),
        service: z.string().optional(),
      }),
      expected: z.union([
        z.object({
          error: z.literal("invalid_config"),
          names: z.array(z.string()),
        }),
        z.object({
          endpoint: z.string(),
          headers: z.record(z.string(), z.string()),
          timeout_ms: z.int(),
          compression: z.enum(["none", "gzip"]),
          resource: z.record(z.string(), z.string()),
        }),
      ]),
    }),
  ),
});

describe("id vectors", () => {
  const ids = Ids.parse(read("otel-ids.json"));
  test("span, continuation, trace and loss ids", () => {
    for (const v of ids.spans)
      expect(spanId(v.branch_id, v.event_id, v.call_id)).toBe(v.span_id);
    for (const v of ids.traces)
      expect(traceId(v.thread_id, v.event_id)).toBe(v.trace_id);
    for (const v of ids.losses)
      expect(lossIds(v.observer, v.thread_id, v.deleted_at)).toEqual({
        traceId: v.trace_id,
        spanId: v.span_id,
      });
  });
  test("an all-zero id gets its last byte set to 1", () => {
    for (const v of ids.nonzero) expect(nonzero(v.digest)).toBe(v.id);
  });
});

type EnvCase = z.infer<typeof Env>["cases"][number];

function optionsOf(v: EnvCase): Options {
  const { endpoint, headers, service } = v.options;
  return {
    ...(endpoint === undefined ? {} : { endpoint }),
    ...(headers === undefined ? {} : { headers }),
    ...(service === undefined ? {} : { service }),
  };
}

/** The ConfigError's message, or "" when nothing was thrown. */
function refusal(run: () => unknown): string {
  try {
    run();
  } catch (e) {
    expect(e).toBeInstanceOf(ConfigError);
    return e instanceof Error ? e.message : "";
  }
  return "";
}

function refused(v: EnvCase, names: readonly string[]): void {
  const message = refusal(() => resolve(optionsOf(v), v.env, "nodejs"));
  for (const name of names) expect(message).toContain(name);
  // Header values are credentials: never in a message.
  const headers = Object.entries(v.env).filter(([name]) =>
    name.endsWith("HEADERS"),
  );
  for (const [, value] of headers) expect(message).not.toContain(value);
}

describe("configuration vectors", () => {
  for (const v of Env.parse(read("otel-env.json")).cases)
    test(v.name, () => {
      if ("error" in v.expected) {
        refused(v, v.expected.names);
        return;
      }
      const got = resolve(optionsOf(v), v.env, "nodejs");
      const { "telemetry.sdk.language": language, ...resource } = got.resource;
      expect(language).toBe("nodejs");
      expect({
        endpoint: got.endpoint,
        headers: got.headers,
        timeout_ms: got.timeoutMs,
        compression: got.compression,
        resource,
      }).toEqual(v.expected);
    });
});
