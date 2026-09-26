import type { ArtifactRef } from "../log";

/** spec/api.json ConfigErrorCode: setup errors, the only failures that throw. */
export type ConfigErrorCode =
  | "invalid_config"
  | "missing_secret"
  | "unknown_preset"
  | "duplicate_name"
  | "capability_missing"
  | "mcp_unreachable"
  | "budget_unenforceable"
  | "permission_rule_invalid"
  | "hosted_tool_unsupported"
  | "egress_policy_unsupported"
  | "transport_fence_unsupported"
  | "handoff_in_team";

/** Thrown at setup only. Every other expected failure is a value. */
export class ConfigError extends Error {
  readonly code: ConfigErrorCode;

  constructor(code: ConfigErrorCode, message: string) {
    super(message);
    this.name = "ConfigError";
    this.code = code;
  }
}

/**
 * Thrown by the result reads (Team.members; lane 21E adds wait, ask and askStatus) when a result
 * the verified log names is missing or corrupt in the artifact store: a broken store invariant,
 * never an expected failure.
 */
export class StoreCorruptError extends Error {
  readonly code: "artifact_missing" | "artifact_corrupt";
  /** The verified ref that failed. */
  readonly ref: ArtifactRef;

  constructor(
    code: "artifact_missing" | "artifact_corrupt",
    ref: ArtifactRef,
    message: string,
  ) {
    super(message);
    this.name = "StoreCorruptError";
    this.code = code;
    this.ref = ref;
  }
}

/**
 * Thrown by Team.events when its `after` names no readable position: a cursor of a later epoch
 * than the feed's, or an offset past the head of the current one (spec/api.json Team.events).
 * The HTTP team stream answers it 400 invalid_cursor.
 */
export class InvalidCursorError extends Error {
  readonly code = "invalid_cursor" as const;

  constructor(message: string) {
    super(message);
    this.name = "InvalidCursorError";
  }
}

/**
 * A dynamic member's recorded choice names a tool or model its template no longer has here: its
 * rebind fails pin_unavailable, unlike a setup that failed for now.
 */
export class Unbound extends ConfigError {}
