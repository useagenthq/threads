// The pinned config's fields a member's thread_started takes; the rest of the config is hashed
// only. Shared by materialize.ts and materialize-host.ts, which build the same schema from it.
// Reference: spec/tools/fixtures/ops_start.py.

/** The `ThreadStartedFields` a pinned config contributes to line 0. */
export const LINE_ZERO = {
  agent_name: true,
  instructions: true,
  model: true,
  model_params: true,
  adapter: true,
  tools: true,
  policy: true,
  sandbox_provider: true,
} as const;
