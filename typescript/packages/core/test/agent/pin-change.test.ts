import { describe, expect, test } from "bun:test";
import { pinChange } from "../../src/agent/pin-change";

// The refusal for a continued thread whose pin changed says how to continue it when the change
// is one prompt caching introduced (lane 10), and says a pin from before resolved settings can't
// be continued.

const started = (
  settings: { readonly [key: string]: unknown },
  ttl = 300_000,
) => ({
  adapter: { settings },
  policy: { permissions: {}, retry: {}, context: { cache_ttl_ms: ttl } },
});

describe("pinChange", () => {
  test("a thread from before prompt caching names promptCache: false", () => {
    expect(pinChange(started({}), started({ prompt_cache: "5m" }))).toBe(
      "this thread was started before prompt caching: pass promptCache: false to anthropic() (prompt_cache=False in Python) to continue it",
    );
  });

  test("a newly declared cache lifetime names the value that continues it", () => {
    expect(pinChange(started({}), started({}, 86_400_000))).toBe(
      "this thread judges cache breaks by a 300000 ms cache lifetime, and the agent's models declare 86400000 ms: set cache_ttl_ms in the agent's context to 300000 to continue it",
    );
  });

  test("a Python pin without its resolved settings starts a new thread", () => {
    const legacy = { adapter: { settings: {} }, policy: {} };
    expect(pinChange(legacy, started({}))).toBe(
      "this thread was started by an older Python release that didn't pin its default permissions, retry and context settings; its config can't be matched now, so start a new thread",
    );
  });

  test("any other change starts a new thread", () => {
    expect(pinChange(started({}), started({}))).toBe(
      "this thread was started with another config; a config change starts a new thread",
    );
  });
});
