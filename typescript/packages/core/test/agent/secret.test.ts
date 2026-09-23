import { describe, expect, test } from "bun:test";
import { inspect } from "node:util";
import { ConfigError, secret } from "../../src";
import { redactSecrets } from "../../src/agent/secret";

describe("secret()", () => {
  test("a reference shows its name, never its value", () => {
    process.env["THREADS_TEST_SECRET_A"] = "sk-value-a";
    const s = secret("THREADS_TEST_SECRET_A");
    expect(s.name).toBe("THREADS_TEST_SECRET_A");
    for (const shown of [
      JSON.stringify({ s }),
      String(s),
      `${s}`,
      inspect(s),
      inspect(s, { showHidden: true, depth: 9 }),
    ])
      expect(shown).not.toContain("sk-value-a");
  });

  test("the value resolves on the host at use time; a missing one is missing_secret", () => {
    const s = secret("THREADS_TEST_SECRET_B");
    delete process.env["THREADS_TEST_SECRET_B"];
    expect(() => s.reveal()).toThrow(ConfigError);
    try {
      s.reveal();
    } catch (error) {
      expect(error instanceof ConfigError && error.code).toBe("missing_secret");
    }
    process.env["THREADS_TEST_SECRET_B"] = "sk-value-b";
    expect(s.reveal()).toBe("sk-value-b");
  });

  test("every revealed value is redacted from text the log would record", () => {
    process.env["THREADS_TEST_SECRET_C"] = "sk-value-c";
    secret("THREADS_TEST_SECRET_C").reveal();
    expect(redactSecrets("token=sk-value-c; again sk-value-c")).toBe(
      "token=[secret THREADS_TEST_SECRET_C]; again [secret THREADS_TEST_SECRET_C]",
    );
    expect(redactSecrets("nothing here")).toBe("nothing here");
  });
});
