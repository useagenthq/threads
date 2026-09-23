import { describe, expect, test } from "bun:test";
import type { Sandbox, SandboxSession } from "../../../src/sandbox";
import { execute, manifestHash } from "../../../src/sandbox";
import { memoryArtifacts } from "../../../src/store/artifacts";
import { code, unwrap } from "../../store/helpers";
import { CTX, changingInputs, type MutableCall } from "../context";
import { created, drained, LOST_CLAIM, run, staleContext } from "./kit";
import type { World } from "./world";

// The provider adapter contract (spec/api.json Sandbox, SandboxSession, F11.3):
// run against every adapter built on the remote kit, each over its own mocked transport.

/** The host secret each adapter is built with; it must never reach the sandbox. */
export const CANARY = "threads-canary-3f9c1d";

export type Contract = {
  readonly sandbox: Sandbox;
  readonly world: World;
  /** Everything the adapter sent toward sandboxes (bodies, URLs), as text. */
  readonly sandboxTraffic: () => string;
};

/** Whether the adapter declares a snapshot boundary it can confirm. */
function capturing(sandbox: Sandbox): boolean {
  return sandbox.info.capture_classes.length > 0;
}

async function lookup(sandbox: Sandbox, key: string) {
  if (sandbox.lookup === undefined) throw new Error("the adapter can look up");
  return unwrap(await sandbox.lookup(key, CTX));
}

export function contractSuite(name: string, make: () => Contract): void {
  describe(`sandbox adapter contract: ${name}`, () => {
    test("exec reports the exit code and keeps stdout and stderr apart", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      expect(await run(box, ["echo", "hi", "there"])).toEqual({
        exit: 0,
        stdout: "hi there\n",
        stderr: "",
      });
      expect(await run(box, ["fail"])).toEqual({
        exit: 3,
        stdout: "",
        stderr: "boom\n",
      });
    });

    test("exec env is exactly what was given; stdin is passed", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      expect((await run(box, ["printenv"], { env: { A: "1" } })).stdout).toBe(
        "A=1\n",
      );
      const stdin = new TextEncoder().encode("from stdin");
      expect((await run(box, ["cat"], { stdin })).stdout).toBe("from stdin");
    });

    test("a caller bug throws and sends nothing: empty command, bad or reserved env name", async () => {
      const { sandbox, world, sandboxTraffic } = make();
      const box = await created(sandbox);
      const machine = world.machine(box.id);
      const before = {
        scripts: machine.scripts.length,
        files: machine.files.size,
        traffic: sandboxTraffic(),
      };
      const stdin = new TextEncoder().encode("never sent");
      const cases = [
        { command: [], env: {}, error: "exec needs a command" },
        {
          command: ["printenv"],
          env: { "BAD NAME": "v" },
          error: "not a shell name",
        },
        {
          command: ["printenv"],
          env: { __t_PATH: "/nowhere" },
          error: "reserved",
        },
      ] as const;
      for (const { command, env, error } of cases)
        await expect(
          box.exec(command, CTX, { processKey: "bug", env, stdin }),
        ).rejects.toThrow(error);
      expect({
        scripts: machine.scripts.length,
        files: machine.files.size,
        traffic: sandboxTraffic(),
      }).toEqual(before);
    });

    test("exec runs what it checked, even if the caller changes it mid-call", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      const call: MutableCall = {
        command: ["cat"],
        options: {
          processKey: "held",
          cwd: "/workspace",
          env: {},
          stdin: new TextEncoder().encode("from stdin"),
        },
      };
      const out = unwrap(
        await box.exec(call.command, changingInputs(call), call.options),
      );
      expect(await drained(out)).toEqual({
        exit: 0,
        stdout: "from stdin",
        stderr: "",
      });
    });

    test("a timeout terminates the key the process started under, whatever the caller changes", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      const terminated = Promise.withResolvers<string>();
      const watched: SandboxSession = {
        ...box,
        terminate: async (processKey, context) => {
          terminated.resolve(processKey);
          return box.terminate(processKey, context);
        },
      };
      const call: MutableCall = {
        command: ["sleep"],
        options: {
          processKey: "k-held",
          cwd: "/workspace",
          env: {},
          timeoutMs: 5,
        },
      };
      const ctx = changingInputs(call);
      const ran = await execute(
        watched,
        call.command,
        ctx,
        call.options,
        memoryArtifacts(),
      );
      expect(code(ran)).toBe("timeout");
      expect(await terminated.promise).toBe("k-held");
    });

    test("terminate kills what the provider tracks but never confirms it", async () => {
      const { sandbox, world } = make();
      expect(sandbox.info.termination).toBe("unconfirmed");
      const box = await created(sandbox);
      const out = unwrap(await box.exec(["spawn"], CTX, { processKey: "k1" }));
      expect(unwrap(await box.terminate("k1", CTX))).toBe("unknown");
      expect((await drained(out)).exit).toBe(137);
      // The descendant outlived the kill: an empty answer would have been a lie.
      expect(world.machine(box.id).procs.has("descendant")).toBe(true);
      expect(unwrap(await box.terminate("k1", CTX))).toBe("unknown");
    });

    test("a timeout is reported as one, and the process is killed best effort", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      // The kill runs in the background: wait for it to settle, not for a clock.
      const stopped = Promise.withResolvers<void>();
      const watched: SandboxSession = {
        ...box,
        terminate: async (processKey, context) => {
          const answer = await box.terminate(processKey, context);
          stopped.resolve();
          return answer;
        },
      };
      const ran = await execute(
        watched,
        ["sleep"],
        CTX,
        { processKey: "k2", timeoutMs: 5 },
        memoryArtifacts(),
      );
      expect(code(ran)).toBe("timeout");
      await stopped.promise;
      expect(world.machine(box.id).procs.has("k2")).toBe(false);
    });

    test("upload and download round-trip bytes; paths fail typed", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      const bytes = new Uint8Array([0, 1, 2, 255]);
      unwrap(await box.upload("dir/a.bin", bytes, CTX));
      expect(unwrap(await box.download("/workspace/dir/a.bin", CTX))).toEqual(
        bytes,
      );
      expect(code(await box.download("missing", CTX))).toBe("not_found");
      expect(code(await box.download("dir", CTX))).toBe("is_directory");
      expect(code(await box.upload("dir", bytes, CTX))).toBe("is_directory");
      expect(code(await box.download("/../../x", CTX))).toBe("invalid_path");
    });

    test("a snapshot is taken only behind a confirmed whole-sandbox boundary", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      unwrap(await box.upload("a.txt", new TextEncoder().encode("one"), CTX));
      const snap = await box.snapshot("op-snap", CTX);
      if (!capturing(sandbox)) {
        expect(sandbox.info.capture_classes).toEqual([]);
        expect(code(snap)).toBe("unavailable");
        expect(world.snapshots.size).toBe(0);
        return;
      }
      const data = unwrap(snap);
      expect(data.capture_class).toBe("filesystem");
      expect(data.manifest_hash).toBe(world.hashOf(data.snapshot_id));
      expect(unwrap(await sandbox.release(data.snapshot_id, CTX))).toBe(
        "released",
      );
      expect(world.snapshots.has(data.snapshot_id)).toBe(false);
    });

    test("a writer running around the capture refuses the snapshot (not_quiescent)", async () => {
      const { sandbox, world } = make();
      if (!capturing(sandbox)) return;
      const box = await created(sandbox);
      unwrap(await box.exec(["writer"], CTX, { processKey: "w" }));
      expect(code(await box.snapshot("op-snap", CTX))).toBe("not_quiescent");
      expect(world.snapshots.size).toBe(0);
    });

    test("restore: the tree is verified, and the child is isolated", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      unwrap(await box.upload("a.txt", new TextEncoder().encode("one"), CTX));
      const ref = world.snapshot(box.id, "seed");
      const child = unwrap(
        await sandbox.restore(ref, world.hashOf(ref), "op-2", CTX),
      );
      expect(child.id).not.toBe(box.id);
      unwrap(await child.upload("a.txt", new TextEncoder().encode("two"), CTX));
      expect(
        new TextDecoder().decode(unwrap(await box.download("a.txt", CTX))),
      ).toBe("one");
    });

    test("a restored tree that fails the manifest is released", async () => {
      const { sandbox, world } = make();
      const box = await created(sandbox);
      const ref = world.snapshot(box.id, "seed");
      const before = world.machines.size;
      const restored = await sandbox.restore(
        ref,
        manifestHash([
          { path: "x", mode: 0o644, size: 1, sha256: "0".repeat(64) },
        ]),
        "op-3",
        CTX,
      );
      expect(code(restored)).toBe("snapshot_manifest_mismatch");
      expect(world.machines.size).toBe(before);
    });

    test("a missing snapshot is snapshot_missing", async () => {
      const { sandbox } = make();
      expect(
        code(await sandbox.restore("nope", manifestHash([]), "op-4", CTX)),
      ).toBe("snapshot_missing");
    });

    test("lookup finds a create by its operation key; attach reattaches by ref", async () => {
      const { sandbox } = make();
      const box = await created(sandbox);
      const found = await lookup(sandbox, "op-1");
      expect(found.status === "found" ? found.value.id : found.status).toBe(
        box.id,
      );
      expect((await lookup(sandbox, "op-x")).status).toBe("not_found_nonfinal");
      expect(unwrap(await sandbox.attach(box.id, CTX)).id).toBe(box.id);
      unwrap(await box.close(CTX));
      expect(code(await sandbox.attach(box.id, CTX))).toBe("not_found");
    });

    test("a refused fence reaches nothing: stale_epoch, or cleanup_claim_lost for gc", async () => {
      const { sandbox, world } = make();
      const stale = staleContext();
      expect(code(await sandbox.create("op-5", stale))).toBe("stale_epoch");
      expect(stale.calls).toBeGreaterThan(0);
      expect(world.creates).toBe(0);
      const box = await created(sandbox);
      const scripts = world.machine(box.id).scripts.length;
      expect(code(await box.exec(["echo"], stale, { processKey: "k" }))).toBe(
        "stale_epoch",
      );
      expect(code(await box.close(LOST_CLAIM))).toBe("cleanup_claim_lost");
      expect(code(await sandbox.release("x", LOST_CLAIM))).toBe(
        "cleanup_claim_lost",
      );
      expect(world.machine(box.id).scripts.length).toBe(scripts);
    });

    test("credentials never enter the sandbox (invariant 4)", async () => {
      const { sandbox, world, sandboxTraffic } = make();
      const box = await created(sandbox);
      await run(box, ["printenv"], { env: { PATH: "/usr/bin" } });
      unwrap(await box.upload("f", new TextEncoder().encode("x"), CTX));
      await box.snapshot("op-snap", CTX);
      unwrap(await box.terminate("b:call-1", CTX));
      for (const machine of world.machines.values())
        expect(machine.everything()).not.toContain(CANARY);
      expect(sandboxTraffic()).not.toContain(CANARY);
    });
  });
}
