import { z } from "zod";
import { sha256Hex } from "../hash";
import type { Binding, Scope } from "../memory/protocol";
import type { SqliteDriver } from "./driver";

// Host-issued bindings (store.sql memory_bindings, knowledge_bindings and // provider_audit). The host, never the provider, decides which records a scope may see: it
// issues a binding before every write or ingest, and before use it looks each returned binding
// up here. A scope label a provider echoes proves nothing, so none is read. Bindings derive from
// the scope and the write key, so a retried write carries the same binding (the same rule, and
// the same bytes, as the Python store: a store is shared by both languages).

export type BindingKind = "memory" | "knowledge";

const TABLE = {
  memory: "memory_bindings",
  knowledge: "knowledge_bindings",
} as const satisfies Record<BindingKind, string>;

const Owned = z.array(z.object({ one: z.literal(1) }));

const digest = (...parts: readonly string[]): string =>
  sha256Hex(parts.join("\u0000")).slice(0, 32);

export class HostBindings {
  readonly #db: SqliteDriver;
  readonly #now: () => number;

  constructor(db: SqliteDriver, now: () => number) {
    this.#db = db;
    this.#now = now;
  }

  /** The binding of the write under `key` in `scope`, durable before the provider sees it. */
  issue(kind: BindingKind, scope: Scope, key: string): Binding {
    const namespace = digest(scope.tenant_id, scope.agent, scope.scope);
    const binding = { namespace, record_id: digest(namespace, key) };
    this.#db.run(
      `INSERT OR IGNORE INTO ${TABLE[kind]} (namespace, record_id, tenant_id, agent, scope)
       VALUES (?, ?, ?, ?, ?)`,
      [
        binding.namespace,
        binding.record_id,
        scope.tenant_id,
        scope.agent,
        scope.scope,
      ],
    );
    return binding;
  }

  /** Whether the host issued `binding` to exactly this scope. */
  owns(kind: BindingKind, scope: Scope, binding: Binding): boolean {
    const rows = this.#db.all(
      `SELECT 1 AS one FROM ${TABLE[kind]} WHERE namespace = ? AND record_id = ?
       AND tenant_id = ? AND agent = ? AND scope = ?`,
      [
        binding.namespace,
        binding.record_id,
        scope.tenant_id,
        scope.agent,
        scope.scope,
      ],
    );
    return Owned.parse(rows).length > 0;
  }

  /** A hit with an unknown or foreign binding: dropped by the caller, recorded here. */
  violation(kind: BindingKind, scope: Scope, binding: Binding): void {
    this.#db.run(
      `INSERT INTO provider_audit (tenant_id, kind, code, namespace, record_id, at)
       VALUES (?, ?, 'scope_violation', ?, ?, ?)`,
      [
        scope.tenant_id,
        kind,
        binding.namespace,
        binding.record_id,
        this.#now(),
      ],
    );
  }
}
