import { describe, expect, test } from "bun:test";
import { err, ok } from "../../../src/result";
import { manifestHash } from "../../../src/sandbox";
import {
  FenceRefused,
  fenceHere,
  type Quiescence,
  remoteSandbox,
  within,
} from "../../../src/sandbox/remote";
import {
  execScript,
  quote,
  sandboxPath,
} from "../../../src/sandbox/remote/scripts";
import { forkCases } from "../../conformance/fork";
import { code, unwrap } from "../../store/helpers";
import { CTX } from "../context";
import { remoteHarness } from "../harness";
import { ledgerSuite } from "../ledger-suite";
import { contractSuite } from "./contract";
import { parseExec, shellWords } from "./machine";
import { memoryDriver } from "./memory-driver";
import { treesSuite } from "./trees";
import { World } from "./world";

// The remote kit over an in-memory driver: the shared contract, the fork cases and the ledger
// crash/takeover suite, before any provider's wire format is involved.

const INFO = {
  provider: "memory",
  egress: "enforced",
  capture_classes: ["filesystem"],
  browser: "none",
  desktop: "none",
} as const;
const EXPIRY = { sandboxMs: 60_000, snapshotMs: null };
const adapter = (world: World) =>
  remoteSandbox(memoryDriver(world), INFO, EXPIRY);

contractSuite("remote kit", () => {
  const world = new World();
  return { sandbox: adapter(world), world, sandboxTraffic: () => "" };
});
treesSuite("remote kit", () => {
  const world = new World();
  return { sandbox: adapter(world), world };
});
forkCases(remoteHarness("remote kit", adapter));
ledgerSuite(remoteHarness("remote kit", adapter));

describe("the fence at the transport", () => {
  test("outside an operation nothing is sent", async () => {
    await expect(fenceHere()).rejects.toThrow(FenceRefused);
  });

  test("inside one, the context decides; a refusal the SDK rethrew as its own is still found", async () => {
    expect(await within(CTX, fenceHere)).toEqual(ok(undefined));
    const stale = {
      ...CTX,
      fence: async () => err({ code: "stale_epoch", message: "lost" } as const),
    };
    const wrapped = await within(stale, async () => {
      try {
        await fenceHere();
      } catch {
        throw new Error("the SDK's own error, no cause");
      }
    });
    expect(wrapped.ok ? undefined : wrapped.error.stale).toEqual({
      code: "stale_epoch",
      message: "lost",
    });
    const other = await within(CTX, async () => {
      throw new Error("provider");
    });
    expect(other.ok ? undefined : other.error.stale).toBeUndefined();
  });
});

describe("the kit's scripts", () => {
  test("quoting survives any value", () => {
    const value = `it's "x" $(rm -rf /) \`y\` \\ $HOME`;
    expect(shellWords(`${quote(value)} ${quote("")}`)).toEqual([value, ""]);
  });

  test("an exec script runs exactly the command, env and cwd it was given", () => {
    const script = execScript({
      command: ["a b", "c'd"],
      cwd: "/workspace/x",
      env: { K: "v w" },
      processKey: "br:call",
      stdin: false,
    });
    expect(parseExec(script)).toEqual({
      cwd: "/workspace/x",
      stdin: "/dev/null",
      env: { K: "v w" },
      argv: ["a b", "c'd"],
    });
  });

  test("paths resolve under /workspace and never above root", () => {
    expect(sandboxPath("a/../b")).toBe("/workspace/b");
    expect(sandboxPath("/etc/x")).toBe("/etc/x");
    expect(sandboxPath("/..")).toBeUndefined();
    expect(sandboxPath("a\0b")).toBeUndefined();
  });
});

describe("snapshot capability by the provider's declared boundary", () => {
  const withQuiescence = (world: World, quiescence: Quiescence) => {
    const driver = memoryDriver(world);
    const take = driver.snapshot?.take;
    if (take === undefined) throw new Error("the memory driver captures");
    return remoteSandbox(
      { ...driver, snapshot: { quiescence, take } },
      INFO,
      EXPIRY,
    );
  };

  test("a whole-sandbox pause records every process frozen", async () => {
    const sandbox = withQuiescence(new World(), "paused");
    const box = unwrap(await sandbox.create("op", CTX));
    expect(sandbox.quiescence).toBe("paused");
    expect(unwrap(await box.snapshot("s", CTX)).quiesced).toEqual({
      frozen: [box.id],
      stopped: [],
      excluded: [],
    });
  });

  test("a whole-sandbox stop records every process stopped", async () => {
    const sandbox = withQuiescence(new World(), "stopped");
    const box = unwrap(await sandbox.create("op", CTX));
    expect(sandbox.info.capture_classes).toEqual(["filesystem"]);
    expect(unwrap(await box.snapshot("s", CTX)).quiesced).toEqual({
      frozen: [],
      stopped: [box.id],
      excluded: [],
    });
  });

  test("an unconfirmed boundary declares no snapshots and captures nothing", async () => {
    const world = new World();
    const sandbox = withQuiescence(world, "unconfirmed");
    expect(sandbox.info.capture_classes).toEqual([]);
    const box = unwrap(await sandbox.create("op", CTX));
    expect(code(await box.snapshot("s", CTX))).toBe("unavailable");
    expect(world.snapshots.size).toBe(0);
  });

  test("no snapshots at all: snapshot and restore fail typed", async () => {
    const world = new World();
    const { snapshot: _, ...driver } = memoryDriver(world);
    const sandbox = remoteSandbox(driver, INFO, EXPIRY);
    expect(sandbox.info.capture_classes).toEqual([]);
    expect(sandbox.quiescence).toBe("none");
    const box = unwrap(await sandbox.create("op", CTX));
    expect(code(await box.snapshot("s", CTX))).toBe("unavailable");
    expect(
      code(await sandbox.restore("s", manifestHash([]), "op-2", CTX)),
    ).toBe("snapshot_missing");
    expect(world.creates).toBe(1);
  });
});
