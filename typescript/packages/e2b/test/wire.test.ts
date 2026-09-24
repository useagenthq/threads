import { expect, test } from "bun:test";
import { within } from "@threads/core/adapter";
import { CTX } from "../../core/test/sandbox/context";
import { envdUrl } from "../src/envd";
import { control } from "../src/rest";
import { sender } from "../src/transport";
import { E2bError } from "../src/wire";
import { CASES, ENVD_URLS } from "./replay";

// The wire shared with Python (spec/conformance/vectors/e2b-wire/cases.json). test/node runs the
// same replay under Node.

for (const c of CASES)
  test(`e2b wire: ${c.name}`, async () => {
    expect(await c.run()).toEqual(c.expect);
  });

test("e2b wire: where each sandbox's envd answers", () => {
  for (const row of ENVD_URLS)
    expect(envdUrl(row.sandbox_id, row.domain)).toBe(row.url);
});

test("a listing whose page token never ends is an error at the page limit, never absence", async () => {
  let pages = 0;
  const endless = async () => {
    pages += 1;
    return new Response("[]", {
      headers: { "content-type": "application/json", "x-next-token": "again" },
    });
  };
  const rest = control(sender(endless), "https://api.e2b.test", () => "key");
  const found = await within(CTX, () => rest.find("op"));
  expect(found.ok ? found.value : found.error.error).toBeInstanceOf(E2bError);
  expect(pages).toBe(100);
});
