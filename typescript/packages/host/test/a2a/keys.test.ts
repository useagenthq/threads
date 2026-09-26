import { describe, expect, test } from "bun:test";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import type { Principal } from "@threads/core/host";
import { z } from "zod";
import { a2aThreadId, bodyHash, sendKey } from "../../src/a2a/keys";

// spec/conformance/vectors/a2a.json, `keys`: the derivations two hosts on one store must agree on.
// The vector is generated from their definitions, not from this implementation's output, so a
// change here that drifts from the contract fails rather than quietly re-pinning itself. Python
// runs the same rows.

const Row = z.strictObject({
  name: z.string(),
  issuer: z.string(),
  tenant: z.string(),
  subject: z.string(),
  agent: z.string(),
  message_id: z.string(),
  context_id: z.string(),
  send_key: z.string(),
  body_hash: z.string(),
  thread_id: z.string(),
});

const VECTOR = join(
  import.meta.dir,
  "../../../../../spec/conformance/vectors/a2a.json",
);

// The other tables belong to the protocol core's own suite, so only `keys` is read here.
const rows = z
  .looseObject({ keys: z.array(Row).min(1) })
  .parse(JSON.parse(readFileSync(VECTOR, "utf8"))).keys;

describe("the exposed side's derivations", () => {
  for (const row of rows)
    test(row.name, () => {
      const principal: Principal = {
        issuer: row.issuer,
        tenant: row.tenant,
        subject: row.subject,
      };
      expect(sendKey(principal, row.agent, row.message_id)).toBe(row.send_key);
      expect(String(a2aThreadId(principal, row.agent, row.context_id))).toBe(
        row.thread_id,
      );
      expect(
        bodyHash(row.agent, {
          messageId: row.message_id,
          role: "ROLE_USER",
          parts: [{ text: "Where is order 1042?" }],
        }),
      ).toBe(row.body_hash);
    });

  test("a context id never names the thread a browser chat of the same shape would", () => {
    // The two derivations differ only by their domain string, which is the whole point of having
    // one: a partner must not be able to reach a signed-in person's chat by guessing a key.
    const principal: Principal = {
      issuer: "partner",
      tenant: "acme",
      subject: "refunds.partner.example",
    };
    const ids = new Set(
      rows.map((r) => a2aThreadId(principal, r.agent, r.context_id)),
    );
    expect(ids.size).toBe(new Set(rows.map((r) => r.context_id)).size);
  });

  test("two principals' equal message ids never share a receipt key", () => {
    const one: Principal = { issuer: "api", tenant: "acme", subject: "a" };
    const two: Principal = { issuer: "api", tenant: "acme", subject: "b" };
    expect(sendKey(one, "support", "m-1")).not.toBe(
      sendKey(two, "support", "m-1"),
    );
  });

  test("a principal part holding the key's separators cannot forge another principal's key", () => {
    // principalKey escapes % and / per part, and each field is length-prefixed, so no crafted
    // subject can make one principal's key read as another's.
    const crafted: Principal = {
      issuer: "api",
      tenant: "acme",
      subject: "b/support/m-1",
    };
    const plain: Principal = { issuer: "api", tenant: "acme", subject: "b" };
    expect(sendKey(crafted, "support", "m-1")).not.toBe(
      sendKey(plain, "support", "m-1"),
    );
  });
});
