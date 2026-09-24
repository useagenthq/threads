import { describe, expect, test } from "bun:test";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { Principal } from "@threads/core/host";
import { z } from "zod";
import { AI_SDK_SCHEMA, aiSdkSchema } from "../../scripts/export-ui-schemas";
import { FOLD_VECTORS, vectors } from "../../scripts/record-ag-ui-fold";
import { AgUiFold, type AgUiMessage } from "../../src/ui/ag-ui-fold";
import { AgUiRunInput, AiSdkChatRequest } from "../../src/ui/bodies";
import type { Chunk } from "../../src/ui/frame";
import {
  ANSWER_SCHEMA,
  APPROVAL_DECISION_SCHEMA,
} from "../../src/ui/interrupts";
import { CHAT_KEY, uiThreadId } from "../../src/ui/key";
import { agUiProjection } from "./stock";

// What the UI routes pin (spec/schema/ui/README.md): the protocol schemas at their versions,
// the interrupt answer schemas as host-api's own, the chat key derivation, and foldAgUi against
// the stock client's recorded folds.

const SPEC = join(import.meta.dir, "../../../../../spec");
const read = (path: string): unknown =>
  JSON.parse(readFileSync(join(SPEC, path), "utf8"));
const Defs = z.object({ $defs: z.record(z.string(), z.unknown()) });

describe("pinned protocol schemas", () => {
  test("the AI SDK chunk schema is ai@7.0.113's own export", async () => {
    expect(readFileSync(AI_SDK_SCHEMA, "utf8")).toBe(await aiSdkSchema());
  });

  test("the AG-UI 1.0 schema is the vendored file, by its sha256", () => {
    const bytes = readFileSync(join(SPEC, "schema/ui/ag-ui-1.0.schema.json"));
    expect(bytes.length).toBe(94_359);
    expect(createHash("sha256").update(bytes).digest("hex")).toBe(
      "4b5c93226838a0e72d88e6c5df20633c686c49fcb75d9be2815c6cbf9e48e71a",
    );
  });
});

describe("host-api shapes", () => {
  const defs = Defs.parse(
    read("schema/host-api/host-api.v1.schema.json"),
  ).$defs;

  test("an interrupt's response schemas are host-api's ApprovalDecision and Answer", () => {
    expect<unknown>(APPROVAL_DECISION_SCHEMA).toEqual(defs["ApprovalDecision"]);
    expect<unknown>(ANSWER_SCHEMA).toEqual(defs["Answer"]);
  });

  const Def = z.object({
    properties: z.record(z.string(), z.unknown()),
    required: z.array(z.string()),
    additionalProperties: z.literal(true),
  });
  for (const [name, schema] of [
    ["AiSdkChatRequest", AiSdkChatRequest],
    ["AgUiRunInput", AgUiRunInput],
  ] as const)
    test(`${name} reads the properties the spec lists, and ignores others`, () => {
      const spec = Def.parse(defs[name]);
      expect(Object.keys(schema.shape).toSorted()).toEqual(
        Object.keys(spec.properties).toSorted(),
      );
      const required = Object.entries(schema.shape)
        .filter(([, field]) => !field.safeParse(undefined).success)
        .map(([key]) => key);
      expect(required.toSorted()).toEqual(spec.required.toSorted());
    });
});

describe("vectors/ui-thread-ids.json", () => {
  const Row = z.object({
    name: z.string(),
    principal: Principal,
    agent: z.string(),
    key: z.string(),
    thread_id: z.string().optional(),
    error: z.literal("invalid_request").optional(),
  });
  for (const row of z
    .array(Row)
    .parse(read("conformance/vectors/ui-thread-ids.json")))
    test(row.name, () => {
      if (row.thread_id === undefined) {
        expect(CHAT_KEY.test(row.key)).toBe(false);
        return;
      }
      expect(CHAT_KEY.test(row.key)).toBe(true);
      expect<string>(uiThreadId(row.principal, row.agent, row.key)).toBe(
        row.thread_id,
      );
    });
});

describe("vectors/ag-ui-fold.json", () => {
  test("is @ag-ui/client@1.0.0's own recording", async () => {
    expect(readFileSync(FOLD_VECTORS, "utf8")).toBe(await vectors());
  });

  const Vector = z.object({
    name: z.string(),
    initial: z.array(z.custom<AgUiMessage>((v) => typeof v === "object")),
    events: z.array(z.custom<Chunk>((v) => typeof v === "object")),
    messages: z.array(z.unknown()),
  });
  for (const v of z
    .array(Vector)
    .parse(read("conformance/vectors/ag-ui-fold.json")))
    test(`foldAgUi: ${v.name}`, () => {
      const fold = new AgUiFold(v.initial);
      for (const e of v.events) fold.apply(e);
      expect(agUiProjection(fold.messages)).toEqual(agUiProjection(v.messages));
    });
});
