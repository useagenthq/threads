import assert from "node:assert/strict";
import { test } from "node:test";
import { envdUrl } from "../../src/envd";
import { CASES, ENVD_URLS } from "../replay";

// The wire shared with Python (spec/conformance/vectors/e2b-wire/cases.json), on Node: malformed and
// wrongly typed REST bodies, a followed page token, and malformed, compressed, truncated or
// oversize envd envelopes, each with its typed outcome.

for (const c of CASES)
  test(`e2b wire: ${c.name}`, async () => {
    assert.deepEqual(await c.run(), c.expect);
  });

test("e2b wire: where each sandbox's envd answers", () => {
  for (const row of ENVD_URLS)
    assert.equal(envdUrl(row.sandbox_id, row.domain), row.url);
});
