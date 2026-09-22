import { expect, test } from "bun:test";
import type { z } from "zod";
import { ContextEdit, OutputValidatedData, UserInputData } from "../../src/log";

// These compile only while a rule's data narrows the parsed type (src/log/narrow.ts).

type Edit = z.output<typeof ContextEdit>;
type Output = z.output<typeof OutputValidatedData>;
type Input = z.output<typeof UserInputData>;

const redactedPart = (edit: Edit): number | undefined =>
  edit.action === "redact" ? edit.part : undefined;
const acceptedValue = (data: Output): z.core.util.JSONType | undefined =>
  data.outcome === "accepted" ? data.value : undefined;
const inputParts = (data: Input): number =>
  "content" in data ? data.content.length : data.text.length;

test("a rule narrows the parsed type", () => {
  const edit = ContextEdit.parse({
    call_id: "c",
    action: "redact",
    part: 0,
    spans: [{ start: 0, end: 1 }],
  });
  expect(redactedPart(edit)).toBe(0);
  const output = OutputValidatedData.parse({
    source_event_id: "0192e000-0000-7000-8000-000000000002",
    schema_sha256: "a".repeat(64),
    outcome: "accepted",
    value: null,
  });
  expect(acceptedValue(output)).toBeNull();
  expect(inputParts(UserInputData.parse({ source: "api", text: "hi" }))).toBe(
    2,
  );
});
