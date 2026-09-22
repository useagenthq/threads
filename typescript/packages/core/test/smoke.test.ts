import { expect, test } from "bun:test";
import { VERSION } from "../src/index";

test("exports VERSION", () => {
  expect(VERSION).toBe("0.0.0");
});
