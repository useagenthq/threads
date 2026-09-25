import { expect, test } from "bun:test";
import { ConfigError } from "@threads/core/adapter";
import { modal } from "../src";

test("modal() is refused at setup with a hint to use the Python adapter", () => {
  expect(() => modal()).toThrow(ConfigError);
  try {
    modal();
  } catch (error) {
    expect(error instanceof ConfigError ? error.code : undefined).toBe(
      "transport_fence_unsupported",
    );
    expect(error instanceof ConfigError ? error.message : undefined).toBe(
      "modal: not available in TypeScript: Modal's JS SDK sends exec and file operations over a channel threads can't fence; use the Python adapter (`from threads.modal import modal`)",
    );
  }
});
