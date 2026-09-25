import { execute, toolRunOf } from "../sandbox/exec";
import { type Builtin, builtin, sessionOf } from "./builtin";
import { BashInput } from "./catalog";

// bash (6, 7): one exec in the sandbox with an empty env, keyed by
// the effect key so recovery can terminate its process group. A timeout is uncertain, never an
// error result; the whole output is spilled at the source by the sandbox layer.

const DEFAULT_TIMEOUT_MS = 120_000;

/** sandbox_local; `builtins` pins it unguarded under open egress. */
export const bash: Builtin = builtin({
  name: "bash",
  input: BashInput,
  effect: "sandbox_local",
  run: async ({ command, timeout_ms }, ctx, env) => {
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    const result = await execute(
      session.value,
      ["bash", "-c", command],
      env.context,
      {
        // Exactly empty: nothing of the host's env, and never a secret (invariant 4).
        env: {},
        timeoutMs: timeout_ms ?? DEFAULT_TIMEOUT_MS,
        processKey: ctx.effectKey,
      },
      env.artifacts,
    );
    return toolRunOf(result);
  },
  terminate: (env) => async (effectKey) => {
    const session = await env.session();
    if (!session.ok) return "unknown";
    const gone = await session.value.terminate(effectKey, env.context);
    return gone.ok ? gone.value : "unknown";
  },
});
