import { ConfigError } from "../agent/errors";
import type { ToolImpl } from "../loop/types";
import { knownEvents } from "../reduce";
import { err } from "../result";
import { ownerContext } from "../sandbox/context";
import type { Sandbox } from "../sandbox/protocol";
import type { ArtifactStore } from "../store/artifacts";
import type { ResourceLedger } from "../store/ledger";
import type { Writer } from "../store/writer";
import { type Builtin, type BuiltinEnv, unguarded } from "./builtin";
import { edit, read, write } from "./files";
import { type Capabilities, gated } from "./gated";
import { notebookEdit } from "./notebook";
import { readToolResult } from "./read-result";
import { glob, grep, ls } from "./search";
import { lazySession } from "./session";
import { bash } from "./shell";

export type { Builtin } from "./builtin";
export type { Capabilities } from "./gated";
export type { SearchBackend, SearchHit } from "./web-search";

/** The run's sandbox session, shared by the tools and the end-of-turn snapshot. */
export type SessionGetter = BuiltinEnv["session"];

// The built-in catalog an agent gets: read_tool_result always, the sandbox
// tools when a sandbox is configured. Sorted by name, pinned before app tools.

/** spec/api.json agent `egress`: absent is deny-all; host allowlists aren't enforced yet. */
export type Egress = readonly string[] | "unenforced";

const byName = (a: Builtin, b: Builtin): number =>
  a.spec.name < b.spec.name ? -1 : 1;

/**
 * The sandbox's own tools: what they change stays inside the sandbox, so a live eval may run
 * them in its throwaway sandbox when egress is deny-all (the computer tool is left out: it can
 * act outside).
 */
export const SANDBOX_TOOLS: ReadonlySet<string> = new Set([
  "bash",
  "edit",
  "glob",
  "grep",
  "ls",
  "notebook_edit",
  "read",
  "write",
]);

export function builtins(
  sandbox: Sandbox | undefined,
  egress: Egress | undefined,
  capabilities: Capabilities = {},
): readonly Builtin[] {
  const extra = gated(capabilities, sandbox);
  if (sandbox === undefined) return [readToolResult, ...extra].toSorted(byName);
  if (Array.isArray(egress) && egress.length > 0)
    throw new ConfigError(
      "egress_policy_unsupported",
      "egress host allowlists are not supported yet; omit egress for deny-all",
    );
  const enforced = sandbox.info.egress === "enforced";
  if (!enforced && egress !== "unenforced")
    throw new ConfigError(
      "egress_policy_unsupported",
      `sandbox ${sandbox.info.provider} can't enforce egress; opt in with egress: "unenforced"`,
    );
  const all = [
    bash,
    edit,
    glob,
    grep,
    ls,
    notebookEdit,
    read,
    readToolResult,
    write,
    ...extra,
  ];
  const denyAll = enforced && egress !== "unenforced";
  return (denyAll ? all : all.map(unguarded)).toSorted(byName);
}

/** The built-ins bound to one run: its writer's lease, the branch's ledger and artifacts. */
export function bindBuiltins(
  sandbox: Sandbox | undefined,
  egress: Egress | undefined,
  run: {
    readonly ledger: ResourceLedger;
    readonly writer: Writer;
    readonly artifacts: ArtifactStore;
  },
  capabilities: Capabilities = {},
): {
  readonly tools: readonly ToolImpl[];
  readonly session: SessionGetter;
  /** L3 restore's framework read; absent without a sandbox. */
  readonly readFile?: (path: string) => Promise<Uint8Array | undefined>;
} {
  const { ledger, writer, artifacts } = run;
  const session: SessionGetter =
    sandbox === undefined
      ? async () => err({ code: "unavailable", message: "no sandbox" })
      : lazySession(ledger, writer, sandbox);
  const env: BuiltinEnv = {
    session,
    context: ownerContext(writer),
    artifacts,
    events: () => knownEvents(writer.chain),
  };
  const tools = builtins(sandbox, egress, capabilities).map((b) => b.bind(env));
  if (sandbox === undefined) return { tools, session };
  const readFile = async (path: string): Promise<Uint8Array | undefined> => {
    const open = await session();
    if (!open.ok) return undefined;
    const got = await open.value.download(path, env.context);
    return got.ok ? got.value : undefined;
  };
  return { tools, session, readFile };
}
