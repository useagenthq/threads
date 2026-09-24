import { describe, expect, test } from "bun:test";
import { err } from "../../../src/result";
import {
  fenceHere,
  remoteSandbox,
  type SandboxDriver,
} from "../../../src/sandbox/remote";
import { code, unwrap } from "../../store/helpers";
import { CTX } from "../context";
import { memoryDriver } from "./memory-driver";
import { World } from "./world";

// A driver declares a final lookup or confirmed termination only with its proof, and the kit
// derives SandboxInfo from what it declares instead of hardcoding it.

const INFO = {
  provider: "memory",
  egress: "enforced",
  browser: "none",
  desktop: "none",
} as const;
const EXPIRY = { sandboxMs: 60_000, snapshotMs: null };
const STALE = {
  ...CTX,
  fence: async () => err({ code: "stale_epoch", message: "lost" } as const),
};

describe("declared overrides", () => {
  test("undeclared, a driver's lookup is nonfinal and its termination unconfirmed", () => {
    const { info } = remoteSandbox(memoryDriver(new World()), INFO, EXPIRY);
    expect([info.lookup, info.termination]).toEqual([
      { create: "nonfinal", snapshot: "none" },
      "unconfirmed",
    ]);
  });

  test("a final lookup is declared with a find whose absence is final", async () => {
    const world = new World();
    const sandbox = remoteSandbox(
      {
        ...memoryDriver(world),
        createLookup: "final",
        find: async (key) => {
          await fenceHere();
          const id = world.find(key);
          return typeof id === "string" && id !== "error"
            ? { status: "found", value: id }
            : { status: "not_found" };
        },
      },
      INFO,
      EXPIRY,
    );
    expect(sandbox.info.lookup.create).toBe("final");
    const lookup = sandbox.lookup;
    if (lookup === undefined) throw new Error("the kit always looks up");
    expect(unwrap(await lookup("never", CTX))).toEqual({
      status: "not_found",
    });
    const made = unwrap(await sandbox.create("k", CTX));
    const found = unwrap(await lookup("k", CTX));
    expect(found.status === "found" ? found.value.id : undefined).toBe(made.id);
  });

  test("confirmed termination answers with the driver's terminate, not the best-effort stop", async () => {
    const asked: [string, string][] = [];
    const sandbox = remoteSandbox(
      {
        ...memoryDriver(new World()),
        termination: "confirmed",
        terminate: async (id, key) => {
          await fenceHere();
          asked.push([id, key]);
          return key === "done" ? "already_exited" : "terminated";
        },
      },
      INFO,
      EXPIRY,
    );
    expect(sandbox.info.termination).toBe("confirmed");
    const box = unwrap(await sandbox.create("k", CTX));
    expect(unwrap(await box.terminate("run", CTX))).toBe("terminated");
    expect(unwrap(await box.terminate("done", CTX))).toBe("already_exited");
    expect(asked).toEqual([
      [box.id, "run"],
      [box.id, "done"],
    ]);
    expect(code(await box.terminate("run", STALE))).toBe("stale_epoch");
  });

  test("neither is declarable without its proof", () => {
    const base = memoryDriver(new World());
    // @ts-expect-error: confirmed termination needs terminate; a best-effort stop proves nothing
    const unproven: SandboxDriver = { ...base, termination: "confirmed" };
    // @ts-expect-error: an undeclared (nonfinal) lookup can't answer the final not_found
    const lying: SandboxDriver = {
      ...base,
      find: async () => ({ status: "not_found" }),
    };
    expect([unproven, lying]).toHaveLength(2);
  });
});
