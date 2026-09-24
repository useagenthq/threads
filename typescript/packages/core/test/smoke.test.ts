import { expect, test } from "bun:test";
import { Principal, VERSION } from "../src/index";

test("exports VERSION", () => {
  expect(VERSION).toBe("0.0.0");
});

test("exports Principal, the schema that parses one", () => {
  const who: Principal = Principal.parse({
    issuer: "api",
    tenant: "local",
    subject: "operator",
  });
  expect(who.subject).toBe("operator");
  expect(
    Principal.safeParse({ issuer: "", tenant: "local", subject: "x" }).success,
  ).toBe(false);
});
