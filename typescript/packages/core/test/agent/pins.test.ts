import { expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import type { ConfigErrorCode } from "../../src/agent";
import type { RunErrorCode } from "../../src/loop";

// Closed code sets written in TS are pinned to the spec that owns them.

const SPEC = join(import.meta.dir, "../../../../../spec");
const read = (path: string): unknown =>
  JSON.parse(readFileSync(join(SPEC, path), "utf8"));

const RUN_ERROR_CODES = [
  "model_unavailable",
  "context_exhausted",
  "max_output",
  "max_turns",
  "output_invalid",
  "input_denied",
  "stop_hook_limit",
  "model_error",
  "content_unsupported",
  "continuation_unsupported",
  "artifact_missing",
  "artifact_corrupt",
  "unmatched_external_op",
  "branch_busy",
  "branch_not_runnable",
] as const satisfies readonly RunErrorCode[];

const CONFIG_ERROR_CODES = [
  "invalid_config",
  "missing_secret",
  "unknown_preset",
  "duplicate_name",
  "capability_missing",
  "mcp_unreachable",
  "budget_unenforceable",
  "permission_rule_invalid",
  "hosted_tool_unsupported",
  "egress_policy_unsupported",
] as const satisfies readonly ConfigErrorCode[];

test("RunErrorCode is host-api RunErrorCode", () => {
  const schema = z
    .object({
      $defs: z.object({
        RunErrorCode: z.object({ enum: z.array(z.string()) }),
      }),
    })
    .parse(read("schema/host-api/host-api.v1.schema.json"));
  expect(schema.$defs.RunErrorCode.enum).toEqual([...RUN_ERROR_CODES]);
});

test("ConfigErrorCode is spec/api.json ConfigErrorCode", () => {
  const api = z
    .object({
      types: z.object({
        ConfigErrorCode: z.object({
          type: z.object({ enum: z.array(z.string()) }),
        }),
      }),
    })
    .parse(read("api.json"));
  expect(api.types.ConfigErrorCode.type.enum).toEqual([...CONFIG_ERROR_CODES]);
});
