import { describe, expect, test } from "bun:test";
import { ConfigError, secret } from "threadsai/adapter";
import { bearer, DEFAULT_TIMEOUT_MS, remote } from "../src/a2a";

// remote() and bearer() are pure config: the card is fetched and pinned later, so a host whose
// partner is unreachable still starts. What is checked here is what a setup error must catch before
// anything is sent, and that a credential is never anything but a header.

describe("remote()", () => {
  test("does no I/O, so a partner that is down does not stop a host starting", () => {
    // If this ever dialled, the test would hang or throw: there is no server at that name.
    const refunds = remote(
      "refunds",
      "https://nothing.invalid/.well-known/agent-card.json",
    );
    expect(refunds.name).toBe("refunds");
    expect(refunds.cardUrl).toBe(
      "https://nothing.invalid/.well-known/agent-card.json",
    );
  });

  test("the defaults are opaque provenance, no declared cost, and a two-minute deadline", () => {
    const refunds = remote("refunds", "https://partner.example/card.json");
    expect(refunds.provenance).toBe("opaque");
    expect(refunds.costPerMessage).toBe(0);
    expect(refunds.timeoutMs).toBe(DEFAULT_TIMEOUT_MS);
    expect(DEFAULT_TIMEOUT_MS).toBe(120_000);
    expect(refunds.auth).toBeUndefined();
  });

  test("a name that is not an agent name is invalid_config, naming the shape", () => {
    for (const name of [
      "Refunds",
      "refunds-desk",
      "1refunds",
      "",
      "refunds ",
    ]) {
      let thrown: unknown;
      try {
        remote(name, "https://partner.example/card.json");
      } catch (error) {
        thrown = error;
      }
      expect(thrown).toBeInstanceOf(ConfigError);
      if (!(thrown instanceof ConfigError)) throw new Error("unreachable");
      expect(thrown.code).toBe("invalid_config");
      expect(thrown.message).toContain("[a-z][a-z0-9_]*");
    }
  });

  test("a card URL that is not https is invalid_config: a card is never fetched in the clear", () => {
    for (const url of [
      "http://partner.example/card.json",
      "file:///tmp/card.json",
      "nonsense",
    ]) {
      let thrown: unknown;
      try {
        remote("refunds", url);
      } catch (error) {
        thrown = error;
      }
      expect(thrown).toBeInstanceOf(ConfigError);
      if (!(thrown instanceof ConfigError)) throw new Error("unreachable");
      expect(thrown.code).toBe("invalid_config");
    }
  });

  test("a deadline that is not a positive integer is invalid_config", () => {
    for (const timeoutMs of [0, -1, 1.5, Number.NaN]) {
      expect(() =>
        remote("refunds", "https://partner.example/card.json", { timeoutMs }),
      ).toThrow(ConfigError);
    }
    expect(
      remote("refunds", "https://partner.example/card.json", {
        timeoutMs: 5_000,
      }).timeoutMs,
    ).toBe(5_000);
  });

  test("a declared cost is whole nanos, as usd() gives, and never negative", () => {
    expect(() =>
      remote("refunds", "https://partner.example/card.json", {
        costPerMessage: -1,
      }),
    ).toThrow(ConfigError);
    expect(() =>
      remote("refunds", "https://partner.example/card.json", {
        costPerMessage: 0.5,
      }),
    ).toThrow(ConfigError);
    // 5 cents in nanos: what one message to this partner costs us, as declared.
    expect(
      remote("refunds", "https://partner.example/card.json", {
        costPerMessage: 50_000_000,
      }).costPerMessage,
    ).toBe(50_000_000);
  });

  test("provenance none sends nothing, and opaque is the default", () => {
    expect(
      remote("refunds", "https://partner.example/card.json", {
        provenance: "none",
      }).provenance,
    ).toBe("none");
  });
});

describe("bearer()", () => {
  test("resolves its secret from the host at use time, not at construction", () => {
    const name = `A2A_TEST_TOKEN_${Date.now()}`;
    const auth = bearer(secret(name));
    expect(auth.kind).toBe("bearer");
    // Nothing was read yet, so an unset variable is not a problem until someone asks.
    process.env[name] = "a-partner-token";
    expect(auth.reveal()).toBe("a-partner-token");
    delete process.env[name];
  });

  test("an unset variable is missing_secret naming the variable, not an empty header", () => {
    const auth = bearer(secret("A2A_TEST_TOKEN_UNSET"));
    let thrown: unknown;
    try {
      auth.reveal();
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(ConfigError);
    if (!(thrown instanceof ConfigError)) throw new Error("unreachable");
    expect(thrown.code).toBe("missing_secret");
    expect(thrown.message).toContain("A2A_TEST_TOKEN_UNSET");
  });
});
