import type { Fetch, SandboxDriver } from "@threads/core/adapter";
import { type Envd, envd, envdUrl } from "./envd";
import { control } from "./rest";
import { sender } from "./transport";
import { type Described, E2bError } from "./wire";

// The remote kit's driver over E2B's REST API and each sandbox's envd, spoken directly
// (rest.ts, envd.ts) so every request leaves through the fenced fetch. The API key only
// authenticates the host's own control-plane requests; a sandbox is created with no env vars,
// and nothing from the host env reaches it (AGENTS invariant 4).

export { OPERATION_KEY } from "./rest";

export type DriverOptions = {
  /** Read per call: the key is resolved at setup, after the factory. */
  readonly apiKey: () => string;
  readonly domain: string;
  readonly template: string;
  readonly timeoutMs: number;
  readonly internet: boolean;
  readonly fetch: Fetch;
};

export function e2bDriver(options: DriverOptions): SandboxDriver {
  const send = sender(options.fetch);
  const rest = control(send, `https://api.${options.domain}`, options.apiKey);
  const timeoutS = Math.max(1, Math.floor(options.timeoutMs / 1000));
  const envds = new Map<string, Envd>();
  const remember = (sandbox: Described): Envd => {
    const url = envdUrl(sandbox.sandboxID, sandbox.domain ?? options.domain);
    const made = envd(send, url, sandbox);
    envds.set(sandbox.sandboxID, made);
    return made;
  };
  /** A live sandbox's envd; after a restart, found again by describing the sandbox. */
  const envdOf = async (id: string): Promise<Envd> => {
    const known = envds.get(id);
    if (known !== undefined) return known;
    const described = await rest.describe(id);
    if (described === null)
      throw new E2bError("not_found", `E2B has no sandbox ${id}`);
    return remember(described);
  };

  return {
    create: async (key, snapshot) => {
      const template = snapshot ?? options.template;
      const made = await rest.create(template, key, {
        timeoutS,
        internet: options.internet,
      });
      if (made !== null) {
        remember(made);
        return { kind: "created", id: made.sandboxID };
      }
      const message = `E2B has no template ${template}`;
      if (snapshot === undefined) throw new E2bError("unavailable", message);
      return { kind: "snapshot_missing", message };
    },
    find: async (key) => {
      const ids = await rest.find(key);
      const [only, ...more] = ids;
      if (only === undefined) return { status: "not_found_nonfinal" };
      if (more.length > 0)
        return {
          status: "unknown",
          reason: `several sandboxes carry ${key}: ${ids.join(", ")}`,
        };
      return { status: "found", value: only };
    },
    exists: async (id) => {
      const described = await rest.describe(id);
      if (described !== null) remember(described);
      return described !== null;
    },
    kill: async (id) => {
      envds.delete(id);
      return (await rest.kill(id)) ? "killed" : "already_gone";
    },
    run: async (id, script, sinks, processKey) =>
      (await envdOf(id)).start(
        { argv: ["/bin/sh", "-c", script], env: {}, cwd: "/", tag: processKey },
        sinks,
      ),
    stopProcess: async (id, processKey) => {
      await (await envdOf(id)).signal(processKey);
    },
    write: async (id, path, data) => (await envdOf(id)).upload(path, data),
    read: async (id, path) => (await envdOf(id)).download(path),
    snapshot: {
      // E2B's snapshot pauses the sandbox, but E2B doesn't document that the pause is a
      // barrier for every descendant and clone, so quiescence stays unconfirmed and the kit
      // takes no snapshots until a live qualification proves it. A restore from an E2B
      // snapshot id still works, verified against the manifest hash.
      quiescence: "unconfirmed",
      // E2B keeps a snapshot until it is deleted.
      take: async (id, key) => ({
        ref: await rest.snapshot(id, `threads-${key}`),
        expiresAt: null,
      }),
    },
    deleteSnapshot: async (ref) =>
      (await rest.deleteTemplate(ref)) ? "released" : "already_gone",
  };
}
