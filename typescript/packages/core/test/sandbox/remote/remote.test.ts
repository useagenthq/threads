import { describe, expect, test } from "bun:test";
import { err, ok } from "../../../src/result";
import { manifestHash } from "../../../src/sandbox";
import {
  FenceRefused,
  fenceHere,
  type Quiescence,
  refusal,
  remoteSandbox,
  within,
} from "../../../src/sandbox/remote";
import {
  execScript,
  parseManifest,
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
forkCases(remoteHarness("remote kit", adapter));
ledgerSuite(remoteHarness("remote kit", adapter));

describe("the fence at the transport", () => {
  test("outside an operation nothing is sent", async () => {
    expect(fenceHere()).rejects.toThrow(FenceRefused);
  });

  test("inside one, the context decides; a wrapped refusal is still found", async () => {
    await within(CTX, fenceHere);
    const stale = {
      ...CTX,
      fence: async () => err({ code: "stale_epoch", message: "lost" } as const),
    };
    let thrown: unknown;
    try {
      await within(stale, fenceHere);
    } catch (error) {
      thrown = error;
    }
    expect(refusal(new Error("sdk", { cause: thrown }))).toEqual({
      code: "stale_epoch",
      message: "lost",
    });
    expect(refusal(new Error("other"))).toBeUndefined();
    expect(await within(CTX, async () => ok(1))).toEqual(ok(1));
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

  test("a manifest is parsed strictly: malformed output is refused", () => {
    const utf8 = new TextEncoder();
    const sha = "a".repeat(64);
    const good = [`644\t3\t${sha}\tb`, `755\t1\t${sha}\ta`, ""].join("\0");
    expect(parseManifest(utf8.encode(good))).toEqual([
      { path: "a", mode: 0o755, size: 1, sha256: sha },
      { path: "b", mode: 0o644, size: 3, sha256: sha },
    ]);
    const bad = [
      `9\t3\t${sha}\tb\0`,
      `644\tx\t${sha}\tb\0`,
      "644\t3\tzz\tb\0",
      `644\t3\t${sha}\t\0`,
    ];
    for (const output of bad)
      expect(parseManifest(utf8.encode(output))).toBeUndefined();
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
    ).toBe("snapshot_restore_failed");
    expect(world.creates).toBe(1);
  });
});
