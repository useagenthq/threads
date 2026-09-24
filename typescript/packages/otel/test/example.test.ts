import { afterEach, describe, expect, test } from "bun:test";
import { mkdtempSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { accepted, type Collector, collector } from "./collector";

// The docs' Observability example runs as printed, against a fake collector.

const EXAMPLE = join(import.meta.dir, "../examples/telemetry.ts");
const PAGE = join(
  import.meta.dir,
  "../../../../docs/content/docs/(guides)/production/observability.mdx",
);

let c: Collector | undefined;
afterEach(async () => {
  await c?.stop();
});

describe("examples/telemetry.ts", () => {
  test("is the Observability page's example, byte for byte", () => {
    expect(readFileSync(PAGE, "utf8")).toContain(readFileSync(EXAMPLE, "utf8"));
  });

  test("runs and exports its turn to the collector the variables name", async () => {
    c = collector();
    const run = Bun.spawn(["bun", EXAMPLE], {
      cwd: mkdtempSync(join(tmpdir(), "threads-otel-example-")),
      env: {
        ...process.env,
        OTEL_EXPORTER_OTLP_ENDPOINT: c.url.replace("/v1/traces", ""),
      },
      stdout: "pipe",
      stderr: "pipe",
    });
    expect(await run.exited).toBe(0);
    expect(await new Response(run.stdout).text()).toBe("2 spans exported\n");
    const names = accepted(c).map((s) => s.name);
    expect(names.toSorted()).toEqual(["chat scripted-1", "invoke_agent agent"]);
    const service = c.received[0]?.text.includes('"stringValue":"support-bot"');
    expect(service).toBe(true);
  });
});
