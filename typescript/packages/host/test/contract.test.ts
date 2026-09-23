import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { z } from "zod";
import * as schemas from "../src/schemas";

// The host's request schemas are the spec's (spec/schema/host-api/host-api.v1.schema.json): the
// same properties, the same required ones, and no extra key admitted.

const SPEC = join(import.meta.dir, "../../../../spec/schema/host-api");
const Def = z.object({
  properties: z.record(z.string(), z.unknown()),
  required: z.array(z.string()).optional(),
  additionalProperties: z.literal(false),
});
const defs = z
  .object({ $defs: z.record(z.string(), z.unknown()) })
  .parse(
    JSON.parse(readFileSync(join(SPEC, "host-api.v1.schema.json"), "utf8")),
  ).$defs;

const PAIRS = {
  StartRunRequest: schemas.StartRunRequest,
  ApprovalDecision: schemas.ApprovalDecision,
  Answer: schemas.Answer,
  ParkedResolution: schemas.ParkedResolution,
  SettingsChange: schemas.SettingsChange,
  ModeChange: schemas.ModeChange,
  ForkRequest: schemas.ForkRequest,
} as const;

describe("request bodies match host-api.v1.schema.json", () => {
  for (const [name, schema] of Object.entries(PAIRS))
    test(name, () => {
      const spec = Def.parse(defs[name]);
      const shape = schema.shape;
      expect(Object.keys(shape).toSorted()).toEqual(
        Object.keys(spec.properties).toSorted(),
      );
      const required = Object.entries(shape)
        .filter(([, field]) => !field.safeParse(undefined).success)
        .map(([key]) => key);
      expect(required.toSorted()).toEqual((spec.required ?? []).toSorted());
      expect(schema.safeParse({ unexpected: 1 }).success).toBe(false);
    });
});

describe("route failures are the openapi x-error-codes", () => {
  const openapi = z
    .object({
      paths: z.record(
        z.string(),
        z.record(
          z.string(),
          z.object({ "x-error-codes": z.array(z.string()) }).partial(),
        ),
      ),
    })
    .parse(JSON.parse(readFileSync(join(SPEC, "openapi.json"), "utf8")));
  test("every /v1 route lists unauthenticated and not_found", () => {
    for (const [path, ops] of Object.entries(openapi.paths))
      for (const op of Object.values(ops))
        if (path.startsWith("/v1/")) {
          expect(op["x-error-codes"]).toContain("unauthenticated");
          expect(op["x-error-codes"]).toContain("not_found");
        }
  });
});
