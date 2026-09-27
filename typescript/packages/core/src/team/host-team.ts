import { createHash } from "node:crypto";
import { BranchId, TeamId, ThreadId } from "../log";

// A tenant's host team ids (spec/schema/README.md, "Teams Phase 2", semantic rule 50), derived so
// every process computes the same three ids: a concurrent lazy open collides on the primary key
// and the loser gets already_open. Reference: spec/tools/fixtures/host_ids.py.

/** 4-byte big-endian UTF-8 length, then the bytes: no two field splits collide. */
export function lengthPrefixed(text: string): Uint8Array {
  const bytes = new TextEncoder().encode(text);
  const out = new Uint8Array(4 + bytes.length);
  new DataView(out.buffer).setUint32(0, bytes.length);
  out.set(bytes, 4);
  return out;
}

/** The UUIDv8 of sha256 over `fields`, each length-prefixed (the chat key's construction). */
export function derivedUuid(fields: readonly string[]): string {
  const parts = fields.map(lengthPrefixed);
  const input = new Uint8Array(parts.reduce((n, f) => n + f.length, 0));
  let at = 0;
  for (const f of parts) {
    input.set(f, at);
    at += f.length;
  }
  const b = new Uint8Array(createHash("sha256").update(input).digest()).slice(
    0,
    16,
  );
  b[6] = ((b[6] ?? 0) & 0x0f) | 0x80;
  b[8] = ((b[8] ?? 0) & 0x3f) | 0x80;
  const hex = Array.from(b, (x) => x.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

const DOMAIN = "threads/host-team";

/** Derived once per (part, tenant): validate_next checks these on every caller mail event. */
const CACHE = new Map<string, string>();

/** One part of a tenant's host team ids: `team`, `log_thread` or `log_branch`. */
export function hostTeamPart(
  part: "team" | "log_thread" | "log_branch",
  tenant: string,
): string {
  const key = `${part}\u0000${tenant}`;
  const known = CACHE.get(key);
  if (known !== undefined) return known;
  const derived = derivedUuid([DOMAIN, part, tenant]);
  CACHE.set(key, derived);
  return derived;
}

/** A tenant's host team: the team id and the thread and branch of its log. */
export type HostTeamIds = {
  readonly teamId: TeamId;
  readonly threadId: ThreadId;
  readonly branchId: BranchId;
};

/** The three ids the tenant derives. A host team has no lead and never closes. */
export function hostTeamIds(tenant: string): HostTeamIds {
  return {
    teamId: TeamId.parse(hostTeamPart("team", tenant)),
    threadId: ThreadId.parse(hostTeamPart("log_thread", tenant)),
    branchId: BranchId.parse(hostTeamPart("log_branch", tenant)),
  };
}
