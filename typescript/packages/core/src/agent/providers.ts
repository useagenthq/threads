import { assertNever } from "../assert-never";
import { type KnownEvent, keyPart, type Principal } from "../log";
import type { Authorization, ToolImpl } from "../loop/types";
import { admitPaths } from "../memory/admit";
import { principalAuthored } from "../memory/context";
import { bindLocalKnowledge } from "../memory/local-knowledge";
import { bindMemory } from "../memory/local-memory";
import type {
  KnowledgeProvider,
  MemoryProvider,
  Scope,
} from "../memory/protocol";
import { knowledgeTools, memoryTools } from "../memory/tools";
import type { ArtifactStore, LogStore } from "../store";
import type { MemoryWrite } from "./pin";

// A run's memory and knowledge: providers bound to the run's store, scopes from host
// config and the verified principal (never a tool argument), and the host's write authority.
// The scope strings are the Python host's, so a shared store keeps one set of bindings.

export type Providers = {
  readonly tools: readonly ToolImpl[];
  /** What a snapshot records as knowledge_revision; undefined without knowledge. */
  readonly revision: () => Promise<number | undefined>;
};

type Config = {
  readonly name: string;
  readonly memory: MemoryProvider | undefined;
  readonly knowledge: KnowledgeProvider | undefined;
};

/**
 * Memory is per principal: never mixed across tenants, agents or users (C8). The parts are
 * escaped, so ("a/b", "c") and ("a", "b/c") never share a scope.
 */
export function memoryScope(agent: string, principal: Principal): Scope {
  return {
    tenant_id: principal.tenant,
    agent,
    scope: `${keyPart(principal.issuer)}/${keyPart(principal.subject)}`,
  };
}

/**
 * The scope a memory call acts in: the current input's principal (the latest user_input or
 * steer), else the run's. In a shared thread each input is answered from its sender's memory.
 */
export function inputMemoryScope(
  agent: string,
  fallback: Principal,
  events: readonly KnownEvent[],
): Scope {
  const input = events.findLast(
    (e) => e.type === "user_input" || e.type === "steer",
  );
  return memoryScope(agent, input?.actor.principal ?? fallback);
}

/** Knowledge is the agent's corpus within the tenant. */
export function knowledgeScope(agent: string, principal: Principal): Scope {
  return { tenant_id: principal.tenant, agent, scope: "knowledge" };
}

export async function bindProviders(
  def: Config,
  run: {
    readonly log: LogStore;
    readonly artifacts: ArtifactStore;
    readonly principal: Principal;
    readonly threadId: string;
    readonly events: () => readonly KnownEvent[];
  },
): Promise<Providers> {
  const { log, principal } = run;
  const bindings = log.bindings;
  const env = (scope: Scope) => ({
    threadId: run.threadId,
    scope,
    bindings,
    events: run.events,
  });
  const memory =
    def.memory === undefined ? undefined : bindMemory(def.memory, log.driver);
  const kScope = knowledgeScope(def.name, principal);
  const local =
    def.knowledge === undefined
      ? undefined
      : bindLocalKnowledge(def.knowledge, log.driver, run.artifacts);
  if (local !== undefined)
    await admitPaths(local.local, local.paths, kScope, bindings);
  const knowledge = local?.local ?? def.knowledge;
  return {
    tools: [
      ...(memory === undefined
        ? []
        : memoryTools(memory, {
            threadId: run.threadId,
            bindings,
            events: run.events,
            // Read at each call: the current input's principal changes within a thread.
            get scope(): Scope {
              return inputMemoryScope(def.name, principal, run.events());
            },
          })),
      ...(knowledge === undefined
        ? []
        : knowledgeTools(knowledge, env(kScope))),
    ],
    revision: async () => {
      if (knowledge === undefined) return undefined;
      const now = await knowledge.revision(kScope);
      return now.ok ? now.value : undefined;
    },
  };
}

const WRITES: ReadonlySet<string> = new Set(["save_memory", "forget_memory"]);

/**
 * after the permission fold: memory_write only makes a write's decision
 * stricter, never turns a deny or ask into an allow. allow_principal holds only for a
 * principal's own turn with nothing untrusted in context, and otherwise asks.
 */
export function withMemoryWrite(
  decided: Authorization,
  write: MemoryWrite,
  tool: string,
  events: readonly KnownEvent[],
): Authorization {
  if (!WRITES.has(tool) || decided.decision === "deny") return decided;
  switch (write) {
    case "deny":
      return { decision: "deny", source: "policy" };
    case "allow":
      return decided;
    case "allow_principal":
      return principalAuthored(events)
        ? decided
        : { decision: "ask", source: "policy" };
    case "ask":
      return { decision: "ask", source: "policy" };
    default:
      return assertNever(write);
  }
}
