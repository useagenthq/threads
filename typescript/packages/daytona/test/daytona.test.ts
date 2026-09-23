import { describe, expect, test } from "bun:test";
import { forkCases } from "../../core/test/conformance/fork";
import { CTX } from "../../core/test/sandbox/context";
import { remoteHarness } from "../../core/test/sandbox/harness";
import { ledgerSuite } from "../../core/test/sandbox/ledger-suite";
import { CANARY, contractSuite } from "../../core/test/sandbox/remote/contract";
import { losesAfter } from "../../core/test/sandbox/remote/kit";
import { World } from "../../core/test/sandbox/remote/world";
import { code, unwrap } from "../../core/test/store/helpers";
import { daytona } from "../src";
import { demux } from "../src/logs";
import { API, daytonaBackend } from "./backend";

// daytona() over its mocked wire: the shared adapter contract, the fork cases and the ledger
// crash/takeover suite, then what is Daytona's own: the fence at its clients' transport, the
// cold snapshot, the log demux and its declarations. No network.

function adapter(world: World, network: "blocked" | "open" = "blocked") {
  const backend = daytonaBackend(world);
  const sandbox = daytona({
    apiKey: CANARY,
    apiUrl: API,
    network,
    fetch: backend.fetch,
    openSocket: backend.open,
    pollMs: 0,
    waitMs: 1000,
  });
  return { sandbox, backend };
}

contractSuite("daytona", () => {
  const world = new World();
  const { sandbox, backend } = adapter(world);
  return { sandbox, world, sandboxTraffic: backend.traffic };
});
forkCases(remoteHarness("daytona", (world) => adapter(world).sandbox));
ledgerSuite(remoteHarness("daytona", (world) => adapter(world).sandbox));

describe("daytona transport", () => {
  test("every client request is fenced: a lease lost after the create sends nothing more", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    expect(code(await sandbox.create("op", losesAfter(1)))).toBe("stale_epoch");
    expect(world.creates).toBe(1);
    expect(backend.traffic()).not.toContain("/process/session");
  });
});

describe("daytona snapshots are cold", () => {
  test("the sandbox is stopped for the capture and started again", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    const box = unwrap(await sandbox.create("op", CTX));
    unwrap(await box.upload("a.txt", new TextEncoder().encode("x"), CTX));
    const snap = unwrap(await box.snapshot("op-s", CTX));
    expect(snap.quiesced).toEqual({
      frozen: [],
      stopped: [box.id],
      excluded: [],
    });
    expect(snap.manifest_hash).toBe(world.hashOf(snap.snapshot_id));
    expect(backend.states.get(box.id)).toBe("started");
    const traffic = backend.traffic();
    expect(traffic.indexOf(`/sandbox/${box.id}/stop`)).toBeLessThan(
      traffic.indexOf(`/sandbox/${box.id}/snapshot`),
    );
    expect(traffic.indexOf(`/sandbox/${box.id}/snapshot`)).toBeLessThan(
      traffic.indexOf(`/sandbox/${box.id}/start`),
    );
  });
});

describe("the log demux", () => {
  test("splits stdout from stderr, markers split across frames, bytes intact", () => {
    const out: number[] = [];
    const err: number[] = [];
    const split = demux({
      stdout: (b) => out.push(...b),
      stderr: (b) => err.push(...b),
    });
    for (const frame of [
      [1, 1],
      [1, 104, 105, 2],
      [2, 2, 255, 0, 1],
      [1],
      [1, 7],
    ])
      split.push(new Uint8Array(frame));
    split.end();
    expect(out).toEqual([104, 105, 7]);
    expect(err).toEqual([255, 0]);
  });
});

describe("daytona declarations", () => {
  test("egress, termination, snapshots, lookup and expiry are declared as Daytona documents them", () => {
    const { sandbox } = adapter(new World());
    expect(sandbox.info).toEqual({
      provider: "daytona",
      egress: "enforced",
      capture_classes: ["filesystem"],
      browser: "none",
      desktop: "none",
      lookup: { create: "nonfinal", snapshot: "none" },
      termination: "unconfirmed",
    });
    expect(sandbox.quiescence).toBe("stopped");
    expect(sandbox.expiry).toEqual({ sandboxMs: 3_600_000, snapshotMs: null });
    expect(adapter(new World(), "open").sandbox.info.egress).toBe("unenforced");
  });

  test("a sandbox is created with no env, the network blocked, and named by its operation key", async () => {
    const { sandbox, backend } = adapter(new World());
    await sandbox.create("op-7", CTX);
    const body = backend
      .traffic()
      .split("\n")
      .find((line) => line.startsWith(`POST ${API}/sandbox `));
    expect(body).toContain('"env":{}');
    expect(body).toContain('"networkBlockAll":true');
    expect(body).toContain('"name":"threads-op-7"');
  });
});
