import { ConfigError } from "../agent/errors";
import type { ToolImpl } from "../loop/types";
import { knownEvents } from "../reduce";
import { err } from "../result";
import { ownerContext } from "../sandbox/context";
import type { Sandbox } from "../sandbox/protocol";
import type { ArtifactStore } from "../store/artifacts";
import type { ResourceLedger } from "../store/ledger";
import type { Writer } from "../store/writer";
import type { Builtin, BuiltinEnv } from "./builtin";
import { edit, read, write } from "./files";
import { readToolResult } from "./read-result";
import { glob, grep } from "./search";
import { lazySession } from "./session";
import { bash } from "./shell";

export type { Builtin } from "./builtin";

/** The run's sandbox session, shared by the tools and the end-of-turn snapshot. */
export type SessionGetter = BuiltinEnv["session"];

// The built-in catalog an agent gets: read_tool_result always, the sandbox
// tools when a sandbox is configured. Sorted by name, pinned before app tools.

/** spec/api.json agent `egress`: absent is deny-all; host allowlists aren't enforced yet. */
export type Egress = readonly string[] | "unenforced";

export function builtins(
  sandbox: Sandbox | undefined,
  egress: Egress | undefined,
): readonly Builtin[] {
  if (sandbox === undefined) return [readToolResult];
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
    bash(enforced && egress !== "unenforced"),
    edit,
    glob,
    grep,
    read,
    readToolResult,
    write,
  ];
  return all.toSorted((a, b) => (a.spec.name < b.spec.name ? -1 : 1));
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
): { readonly tools: readonly ToolImpl[]; readonly session: SessionGetter } {
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
  return { tools: builtins(sandbox, egress).map((b) => b.bind(env)), session };
}
