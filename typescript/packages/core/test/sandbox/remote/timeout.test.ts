import { describe, expect, test } from "bun:test";
import { execute, toolRunOf } from "../../../src/sandbox/exec";
import { remoteSession } from "../../../src/sandbox/remote/session";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { code } from "../../store/helpers";
import { CTX } from "../context";
import { memoryDriver } from "./memory-driver";
import { World } from "./world";

// once the exec deadline fires, execute reports a timeout at once, whether or not
// the best-effort kill finishes, and the tool result path turns it into effect_unknown.

function session(killCompletes: boolean) {
  const exit = Promise.withResolvers<number>();
  let stops = 0;
  const driver = {
    ...memoryDriver(new World()),
    run: async () => ({ exit: exit.promise }),
    stopProcess: async () => {
      stops += 1;
      if (killCompletes) exit.resolve(137);
    },
  };
  return {
    box: remoteSession(driver, "memory", "sandbox-test"),
    stops: () => stops,
    end: () => exit.resolve(137),
  };
}

const PAST = Symbol("still pending");

describe("the exec deadline", () => {
  for (const killCompletes of [true, false])
    test(`reports timeout, not a normal exit, when the kill ${killCompletes ? "ends the process (137)" : "never finishes"}`, async () => {
      const s = session(killCompletes);
      const ran = execute(
        s.box,
        ["sleep", "60"],
        CTX,
        { processKey: "b:c", timeoutMs: 5 },
        memoryArtifacts(),
      );
      const past = Bun.sleep(100).then(() => PAST);
      const result = await Promise.race([ran, past]);
      expect(typeof result === "symbol" ? "pending" : code(result)).toBe(
        "timeout",
      );
      expect(s.stops()).toBeGreaterThan(0);
      s.end();
    });

  test("covers the start too: an exec that never starts is a timeout, as in Python", async () => {
    const driver = {
      ...memoryDriver(new World()),
      run: () => new Promise<never>(() => undefined),
    };
    const ran = execute(
      remoteSession(driver, "memory", "sandbox-test"),
      ["true"],
      CTX,
      { processKey: "b:c", timeoutMs: 5 },
      memoryArtifacts(),
    );
    const past = Bun.sleep(100).then(() => PAST);
    const result = await Promise.race([ran, past]);
    expect(typeof result === "symbol" ? "pending" : code(result)).toBe(
      "timeout",
    );
  });

  test("the tool result path records a timeout as effect_unknown, never a result", () => {
    expect(
      toolRunOf({ ok: false, error: { code: "timeout", message: "t" } }),
    ).toEqual({ kind: "unknown", reason: "timeout" });
    expect(
      toolRunOf({ ok: false, error: { code: "unavailable", message: "u" } }),
    ).toEqual({ kind: "unknown", reason: "transport_error" });
    expect(
      toolRunOf({ ok: false, error: { code: "stale_epoch", message: "s" } }),
    ).toEqual({ kind: "not_sent" });
    expect(
      toolRunOf({
        ok: true,
        value: { exit_code: 2, stdout: "o", stderr: "e", truncated: false },
      }),
    ).toEqual({
      kind: "done",
      output: JSON.stringify({
        exit_code: 2,
        stdout: "o",
        stderr: "e",
        truncated: false,
      }),
      isError: true,
    });
  });
});
