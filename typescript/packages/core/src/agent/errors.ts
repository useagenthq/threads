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
