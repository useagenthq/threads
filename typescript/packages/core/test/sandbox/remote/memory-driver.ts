import type { SandboxDriver } from "../../../src/sandbox/remote";
import { fenceHere } from "../../../src/sandbox/remote";
import type { World } from "./world";

// The remote kit's driver straight over a World, each call behind the fence as a real
// transport's send would be: for testing remoteSandbox without any provider's wire format.

export function memoryDriver(world: World): SandboxDriver {
  const send = async <T>(call: () => T): Promise<T> => {
    await fenceHere();
    return call();
  };
  return {
    create: (key, snapshot) =>
      send(() => {
        const made = world.create(key, snapshot);
        if ("missing" in made)
          return { kind: "snapshot_missing", message: `no ${snapshot}` };
        if (made.lost) throw new Error("the connection was reset");
        return { kind: "created", id: made.id };
      }),
    find: (key) =>
      send(() => {
        const id = world.find(key);
        if (id === "error") throw new Error("lookup failed");
        return id === undefined
          ? { status: "not_found_nonfinal" }
          : { status: "found", value: id };
      }),
    exists: (id) => send(() => world.machines.has(id)),
    kill: (id) => send(() => (world.kill(id) ? "killed" : "already_gone")),
    run: (id, script, sinks, key) =>
      send(() => world.machine(id).start(script, sinks, key)),
    stopProcess: (id, key) => send(() => world.machine(id).stop(key)),
    write: (id, path, data) => send(() => world.machine(id).write(path, data)),
    read: (id, path) => send(() => world.machine(id).read(path)),
    snapshot: (id, key) =>
      send(() => ({ ref: world.snapshot(id, `snap-${key}`), expiresAt: null })),
    deleteSnapshot: (ref) =>
      send(() => (world.deleteSnapshot(ref) ? "released" : "already_gone")),
  };
}
