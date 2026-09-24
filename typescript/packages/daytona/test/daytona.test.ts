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
import { framed, unhex } from "../src/framing";
import { demux } from "../src/logs";
import { API, daytonaBackend } from "./backend";

// daytona() over its mocked wire: the shared adapter contract, the fork cases and the ledger
// crash/takeover suite, then what is Daytona's own: the fence at its clients' transport, the
// cold snapshot, the log demux and its declarations. No network.

function adapter(world: World, allowInternet = false) {
  const backend = daytonaBackend(world);
  const sandbox = daytona({
    apiKey: CANARY,
    apiUrl: API,
    ...(allowInternet ? { allowInternet } : {}),
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

describe("exec output is exact bytes", () => {
  async function exact(out: Uint8Array, err: Uint8Array) {
    const { sandbox } = adapter(new World());
    const box = unwrap(await sandbox.create("op", CTX));
    const output = unwrap(
      await box.exec(["bytes", out.toHex(), err.toHex()], CTX, {
        processKey: "k",
      }),
    );
    const [stdout, stderr] = await Promise.all([
      Array.fromAsync(output.stdout),
      Array.fromAsync(output.stderr),
    ]);
    expect(await output.exit_code).toBe(0);
    expect([...stdout.flatMap((c) => [...c])]).toEqual([...out]);
    expect([...stderr.flatMap((c) => [...c])]).toEqual([...err]);
  }

  test("marker bytes inside real output survive", async () => {
    await exact(
      new Uint8Array([65, 2, 2, 2, 66]),
      new Uint8Array([1, 1, 1, 67, 2, 2]),
    );
  });

  test("every byte value, in any order, on both streams", async () => {
    const all = Uint8Array.from({ length: 256 }, (_, i) => i);
    for (const seed of [1, 7, 31]) {
      const shuffled = all.toSorted(
        (a, b) => ((a * seed) % 257) - ((b * seed) % 257),
      );
      await exact(shuffled, shuffled.toReversed());
    }
  });
});

describe("the framing wrapper, in a real shell", () => {
  test("hex-frames each stream, keeps the exit code, and decodes back exactly", () => {
    const all = Uint8Array.from({ length: 256 }, (_, i) => i);
    const script = `printf '${[...all].map((b) => `\\${b.toString(8).padStart(3, "0")}`).join("")}'; printf 'A\\002\\002\\002B' >&2; exit 7`;
    const ran = Bun.spawnSync(["sh", "-c", framed(script)]);
    const decode = (bytes: Uint8Array) => {
      const out: number[] = [];
      const split = unhex((b) => out.push(...b));
      // Split mid-pair to prove a digit pair across chunks is held.
      split.push(bytes.subarray(0, 7));
      split.push(bytes.subarray(7));
      return out;
    };
    expect(ran.exitCode).toBe(7);
    expect(decode(ran.stdout)).toEqual([...all]);
    expect(decode(ran.stderr)).toEqual([65, 2, 2, 2, 66]);
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

const createBody = (traffic: string): string | undefined =>
  traffic.split("\n").find((line) => line.startsWith(`POST ${API}/sandbox `));

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
    expect(adapter(new World(), true).sandbox.info.egress).toBe("unenforced");
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

  test("defaults: a one-hour lifetime, the network blocked, Daytona's snapshot and region", async () => {
    const { sandbox, backend } = adapter(new World());
    unwrap(await sandbox.create("op-9", CTX));
    const body = createBody(backend.traffic());
    expect(body).toContain('"ttlMinutes":60');
    expect(body).toContain('"networkBlockAll":true');
    expect(body).not.toContain('"snapshot"');
    expect(body).not.toContain('"target"');
    expect(sandbox.expiry.sandboxMs).toBe(3_600_000);
  });

  test("snapshot, target and lifetimeMs are sent as given; a lifetime rounds up to a minute", async () => {
    const backend = daytonaBackend(new World());
    const sandbox = daytona({
      apiKey: CANARY,
      apiUrl: API,
      fetch: backend.fetch,
      openSocket: backend.open,
      target: "eu",
      lifetimeMs: 90_000,
      pollMs: 0,
    });
    unwrap(await sandbox.create("op-10", CTX));
    const body = createBody(backend.traffic());
    expect(body).toContain('"target":"eu"');
    expect(body).toContain('"ttlMinutes":2');
    expect(sandbox.expiry.sandboxMs).toBe(90_000);
    const named = daytona({
      apiKey: CANARY,
      apiUrl: API,
      fetch: backend.fetch,
      openSocket: backend.open,
      snapshot: "my-snapshot",
      pollMs: 0,
    });
    await named.create("op-11", CTX);
    expect(backend.traffic()).toContain('"snapshot":"my-snapshot"');
  });

  test("a sandbox is private and can't outlive a leak: finite auto-stop, then auto-delete", async () => {
    const { sandbox, backend } = adapter(new World());
    unwrap(await sandbox.create("op", CTX));
    const body = backend
      .traffic()
      .split("\n")
      .find((line) => line.startsWith(`POST ${API}/sandbox `));
    expect(body).toContain('"public":false');
    expect(body).toContain('"autoStopInterval":60');
    expect(body).toContain('"autoDeleteInterval":60');
    const backend2 = daytonaBackend(new World());
    const custom = daytona({
      apiKey: CANARY,
      apiUrl: API,
      fetch: backend2.fetch,
      openSocket: backend2.open,
      autoStopMinutes: 5,
      pollMs: 0,
    });
    unwrap(await custom.create("op", CTX));
    expect(backend2.traffic()).toContain('"autoStopInterval":5');
    for (const autoStopMinutes of [0, -1, 1.5])
      expect(() => daytona({ apiKey: CANARY, autoStopMinutes })).toThrow(
        "autoStopMinutes",
      );
  });
});

describe("the hosted Daytona, as seen live", () => {
  test("the toolbox runs as a non-root user: create makes /workspace with sudo", async () => {
    const world = new World();
    const { sandbox } = adapter(world);
    const box = unwrap(await sandbox.create("op", CTX));
    unwrap(await box.upload("a.txt", new TextEncoder().encode("x"), CTX));
    expect(world.creates).toBe(1);
  });

  test("a create that reports failure leaves a sandbox its lookup finds; a retry creates nothing new", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    backend.failPrepares = 1;
    expect(code(await sandbox.create("op", CTX))).toBe("unavailable");
    if (sandbox.lookup === undefined) throw new Error("daytona looks up");
    const found = unwrap(await sandbox.lookup("op", CTX));
    expect(found.status).toBe("found");
    const again = unwrap(await sandbox.create("op", CTX));
    expect(found.status === "found" && found.value.id).toBe(again.id);
    expect(world.creates).toBe(1);
  });
});
