import { describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { CTX } from "../../core/test/sandbox/context";
import { contractSuite } from "../../core/test/sandbox/remote/contract";
import { losesAfter } from "../../core/test/sandbox/remote/kit";
import { treesSuite } from "../../core/test/sandbox/remote/trees";
import { World } from "../../core/test/sandbox/remote/world";
import { code, unwrap } from "../../core/test/store/helpers";
import { docker } from "../src";
import { PIDS_LIMIT } from "../src/create";
import { demux } from "../src/exec";
import { containerName, shortHash } from "../src/names";
import { type Arch, SUPERVISOR_SHA256, supervisorBinary } from "../src/pins";
import { resolveSocket, UNREACHABLE } from "../src/socket";
import { untar } from "../src/tar";
import { dockerBackend } from "./engine";

// docker() over a mocked Engine API: the shared adapter contract and the tree capability,
// then what is Docker's own — the fence at every request, a 409 read as this key's own
// create, a dropped limit that fails the create, the pinned supervisor, and container output
// as a trust boundary. No daemon, no network.

/** The binaries are build output (scripts/build-supervisor.sh), so a create needs them. */
const ARCHES: readonly Arch[] = ["arm64", "amd64"];
const ARCH = ARCHES.find((a) =>
  existsSync(new URL(`../bin/supervise-linux-${a}`, import.meta.url)),
);

function adapter(world: World, allowInternet = false) {
  const backend = dockerBackend(world);
  backend.architecture = ARCH ?? "arm64";
  const sandbox = docker({
    ...(allowInternet ? { allowInternet } : {}),
    fetch: backend.fetch,
  });
  return { sandbox, backend };
}

if (ARCH !== undefined) {
  contractSuite("docker", () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    return {
      sandbox,
      world,
      sandboxTraffic: backend.traffic,
      processName: shortHash,
      snapshots: false,
    };
  });
  treesSuite("docker", () => {
    const world = new World();
    return { sandbox: adapter(world).sandbox, world };
  });
}

// The fork cases and the ledger suite both drive Sandbox.restore, which docker answers
// snapshot_missing until 16C's host trees land; they join this file with them.

const only = (traffic: string, needle: string) =>
  traffic.split("\n").filter((line) => line.includes(needle));

describe.skipIf(ARCH === undefined)("docker transport", () => {
  test("every request is fenced: a lease lost after the create sends nothing more", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    // The image inspect, then the container create; the lease is gone before the injection.
    expect(code(await sandbox.create("op", losesAfter(2)))).toBe("stale_epoch");
    expect(world.creates).toBe(1);
    expect(backend.traffic()).not.toContain("/archive");
    expect(backend.traffic()).not.toContain("/exec");
  });

  test("a 409 on create is this key's own earlier one: the name is used as it stands", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    const first = unwrap(await sandbox.create("op-409", CTX));
    const again = unwrap(await sandbox.create("op-409", CTX));
    expect<string>(again.id).toBe(first.id);
    expect<string>(again.id).toBe(containerName("op-409"));
    expect(world.creates).toBe(1);
    expect(only(backend.traffic(), "/containers/create")).toHaveLength(2);
  });

  test("a create whose Warnings name a dropped limit fails, and leaves nothing behind", async () => {
    const world = new World();
    const backend = dockerBackend(world);
    backend.architecture = ARCH ?? "arm64";
    backend.warnings = ["Your kernel does not support CPU cfs quota"];
    const sandbox = docker({ cpus: 2, fetch: backend.fetch });
    const made = await sandbox.create("op", CTX);
    expect(code(made)).toBe("unavailable");
    expect(made.ok ? "" : made.error.message).toBe(
      "Docker can't enforce the CPU limit here (rootless Docker needs cgroup v2 delegation)",
    );
    expect(backend.boxes.size).toBe(0);
    expect(backend.volumes.size).toBe(0);
  });

  test("an inspect that shows a requested limit unset fails the create the same way", async () => {
    const backend = dockerBackend(new World());
    backend.architecture = ARCH ?? "arm64";
    backend.dropsMemory = true;
    const sandbox = docker({ memoryMb: 64, fetch: backend.fetch });
    const made = await sandbox.create("op", CTX);
    expect(made.ok ? "" : made.error.message).toBe(
      "Docker can't enforce the memory limit here (rootless Docker needs cgroup v2 delegation)",
    );
    expect(backend.boxes.size).toBe(0);
  });

  test("the supervisor is injected only under its pinned sha256, and a mismatch is refused", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    const box = unwrap(await sandbox.create("op", CTX));
    expect(backend.boxes.get(box.id)?.supervisor).toBe(
      SUPERVISOR_SHA256[ARCH ?? "arm64"],
    );
    // The check is the pin itself: an architecture this package ships nothing for is refused
    // before any byte is injected.
    // @ts-expect-error: only the two architectures this package ships are Arch
    expect(() => supervisorBinary("riscv")).toThrow("ships no supervisor");
  });
});

describe.skipIf(ARCH === undefined)(
  "container output is a trust boundary",
  () => {
    const sinks = { stdout: () => undefined, stderr: () => undefined };

    test("a frame header that isn't Docker's is a typed failure, not a guess", () => {
      const frames = demux(sinks);
      expect(() =>
        frames.push(new Uint8Array([7, 0, 0, 0, 0, 0, 0, 1, 65])),
      ).toThrow("unknown frame type 7");
      expect(() =>
        demux(sinks).push(new Uint8Array([1, 0, 0, 0, 255, 255, 255, 255])),
      ).toThrow("claims a frame of 4294967295 bytes");
      const held = demux(sinks);
      held.push(new Uint8Array([1, 0, 0, 0, 0, 0, 0, 4, 65]));
      expect(() => held.end()).toThrow("ends inside a frame");
    });

    test("a record the supervisor didn't write fails the terminate, typed", async () => {
      const world = new World();
      const { sandbox, backend } = adapter(world);
      const box = unwrap(await sandbox.create("op", CTX));
      unwrap(await box.exec(["sleep"], CTX, { processKey: "k" }));
      const records = backend.boxes.get(box.id)?.records;
      const record = records?.get(shortHash("k"));
      if (records === undefined || record === undefined)
        throw new Error("the supervised run wrote a record");
      // state is a closed set: "wedged" is not one of the four the supervisor writes.
      records.set(shortHash("k"), { ...record, state: "running", key: "" });
      const answer = await box.terminate("k", CTX);
      expect(code(answer)).toBe("unavailable");
      expect(answer.ok ? "" : answer.error.message).toContain("malformed");
    });

    test("an archive that isn't a tar, and a --check that isn't its JSON, are unavailable", async () => {
      expect(() => untar(new Uint8Array(512).fill(0x41))).toThrow("bad size");
      const world = new World();
      const backend = dockerBackend(world);
      backend.architecture = ARCH ?? "arm64";
      const broken = docker({
        fetch: async (input, init) => {
          const res = await backend.fetch(input, init);
          // The --check exec's output stream, with one frame of something else.
          if (
            !String(input).includes("/exec/") ||
            !String(input).endsWith("/start")
          )
            return res;
          await res.arrayBuffer();
          const body = new TextEncoder().encode("not json\n");
          const frame = new Uint8Array(8 + body.length);
          frame[0] = 1;
          new DataView(frame.buffer).setUint32(4, body.length);
          frame.set(body, 8);
          return new Response(frame);
        },
      });
      const made = await broken.create("op", CTX);
      expect(code(made)).toBe("unavailable");
      expect(made.ok ? "" : made.error.message).toContain("isn't JSON");
    });
  },
);

describe("docker declarations", () => {
  test("egress, termination, snapshots and lookup are declared as Docker enforces them", () => {
    expect(docker().info).toEqual({
      provider: "docker",
      egress: "enforced",
      capture_classes: [],
      browser: "none",
      desktop: "none",
      lookup: { create: "nonfinal", snapshot: "none" },
      termination: "confirmed",
    });
    expect(docker({ allowInternet: true }).info.egress).toBe("unenforced");
    // A container doesn't die on its own, and docker() takes no snapshots (16C's host trees).
    expect(docker().expiry).toEqual({ sandboxMs: null, snapshotMs: null });
    expect(docker().quiescence).toBe("none");
  });

  // The create request is what this checks, so it stands whether or not the injection that
  // follows it has the binaries (they are build output).
  test("defaults: the pinned node:22-bookworm digest, no internet and no limits", async () => {
    const world = new World();
    const { sandbox, backend } = adapter(world);
    await sandbox.create("op-d", CTX);
    const body = only(backend.traffic(), "/containers/create?")[0] ?? "";
    expect(body).toContain(
      '"Image":"node:22-bookworm@sha256:363e1587494626837fa7f9a23bdb453d13b0ff3c67c705c2805cfc69c2d2fad7"',
    );
    expect(body).toContain('"NetworkMode":"none"');
    expect(body).toContain(`"PidsLimit":${PIDS_LIMIT}`);
    expect(body).not.toContain("NanoCpus");
    expect(body).not.toContain('"Memory"');
    expect(body).toContain('"ReadonlyRootfs":true');
    expect(body).toContain('"CapDrop":["ALL"]');
    // Nothing but PATH: the image's own env reaches no command (invariant 4).
    expect(body).toContain(
      '"Env":["PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]',
    );
    const limited = dockerBackend(new World());
    limited.architecture = ARCH ?? "arm64";
    await docker({ cpus: 2, memoryMb: 4096, fetch: limited.fetch }).create(
      "op-l",
      CTX,
    );
    const withLimits = only(limited.traffic(), "/containers/create?")[0] ?? "";
    expect(withLimits).toContain('"NanoCpus":2000000000');
    expect(withLimits).toContain('"Memory":4294967296');
    expect(withLimits).toContain('"MemorySwap":4294967296');
    const open = dockerBackend(new World());
    open.architecture = ARCH ?? "arm64";
    await docker({ allowInternet: true, fetch: open.fetch }).create(
      "op-i",
      CTX,
    );
    expect(only(open.traffic(), "/containers/create?")[0]).not.toContain(
      "NetworkMode",
    );
  });

  test("a limit Docker can't express is invalid_config", () => {
    for (const cpus of [0, -1, -0.5])
      expect(() => docker({ cpus })).toThrow(
        `docker: cpus must be greater than 0, not ${cpus}`,
      );
    for (const memoryMb of [8, 63, 128.5])
      expect(() => docker({ memoryMb })).toThrow(
        `docker: memoryMb must be an integer of at least 64, not ${memoryMb}`,
      );
    expect(() => docker({ cpus: -1 })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
    for (const host of [
      "tcp://127.0.0.1:2375",
      "ssh://box",
      "npipe:////./pipe/x",
    ])
      expect(() => resolveSocket({ DOCKER_HOST: host })).toThrow(
        `docker: DOCKER_HOST must be a unix:// socket, not ${host}`,
      );
    expect(() => resolveSocket({ DOCKER_HOST: "tcp://x" })).toThrow(
      expect.objectContaining({ code: "invalid_config" }),
    );
  });

  test("no socket is docker_unreachable, and nothing is connected", async () => {
    expect(() => resolveSocket({}, () => false)).toThrow(
      expect.objectContaining({
        code: "docker_unreachable",
        message: UNREACHABLE,
      }),
    );
    expect(UNREACHABLE).toBe(
      "Docker isn't running (looked for /var/run/docker.sock, ~/.docker/run/docker.sock): start Docker, set DOCKER_HOST, or pass another sandbox (devSandbox(), e2b())",
    );
    // setup() only looks; it opens no connection, so the transport stays untouched.
    const backend = dockerBackend(new World());
    const sandbox = docker({ fetch: backend.fetch });
    const had = process.env["DOCKER_HOST"];
    process.env["DOCKER_HOST"] = "unix:///var/run/docker.sock";
    try {
      await sandbox.setup?.();
    } finally {
      if (had === undefined) delete process.env["DOCKER_HOST"];
      else process.env["DOCKER_HOST"] = had;
    }
    expect(backend.traffic()).toBe("");
  });

  test("the pinned sha256 values are the ones scripts/build-supervisor.sh wrote", async () => {
    const pins: unknown = await Bun.file(
      new URL("../../../../docker/supervise/binaries.json", import.meta.url),
    ).json();
    expect(pins).toEqual({
      "linux-amd64": SUPERVISOR_SHA256.amd64,
      "linux-arm64": SUPERVISOR_SHA256.arm64,
    });
  });
});
