import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { CASES_DIR } from "./cases";
import { caseSchema } from "./schema";

// The test-only case.schema.json validator accepts the corpus and rejects a broken file, so a
// saved case checked with it is checked against the real schema.

const json = (path: string): unknown =>
  JSON.parse(readFileSync(join(CASES_DIR, path), "utf8"));

describe("case.schema.json validator", () => {
  test("accepts a stub case of the corpus and rejects a malformed one", () => {
    const dir = "stub-occurrence-order";
    expect(caseSchema("Case").safeParse(json(`${dir}/case.json`)).success).toBe(
      true,
    );
    expect(
      caseSchema("Expected").safeParse(json(`${dir}/expected.threads-ts.json`))
        .success,
    ).toBe(true);
    expect(
      caseSchema("StubScript").safeParse(json(`${dir}/stubs.json`)).success,
    ).toBe(true);
    expect(
      caseSchema("ModelScript").safeParse(json(`${dir}/model.json`)).success,
    ).toBe(true);
    expect(
      caseSchema("Case").safeParse({ name: "Bad Name", kind: "stub" }).success,
    ).toBe(false);
    expect(caseSchema("Expected").safeParse({ outcome: "maybe" }).success).toBe(
      false,
    );
  });
});
