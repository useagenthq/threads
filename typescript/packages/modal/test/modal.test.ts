import { expect, test } from "bun:test";
import { ConfigError } from "@threads/core/adapter";
import { modal } from "../src";

test("modal() is refused at setup: its command router can't be fenced", () => {
  expect(() => modal()).toThrow(ConfigError);
  try {
    modal();
  } catch (error) {
    expect(error instanceof ConfigError ? error.code : undefined).toBe(
      "transport_fence_unsupported",
    );
  }
});
