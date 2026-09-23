import { z } from "zod";
import { execute, toolRunOf } from "../sandbox/exec";
import { type Builtin, builtin, sessionOf } from "./builtin";

// bash: one exec in the sandbox with an empty env, keyed by
// the effect key so recovery can terminate its process group. A timeout is uncertain, never an
// error result; the whole output is spilled at the source by the sandbox layer.

const DEFAULT_TIMEOUT_MS = 120_000;

const Input = z.strictObject({
  command: z.string().min(1).describe("A POSIX shell command, run with sh -c."),
  timeout_ms: z.int().min(1).max(600_000).optional(),
});

/**
 * sandbox_local only under enforced deny-all egress; otherwise a command may
 * reach the outside world, so it is unguarded and uncertainty parks.
 */
export function bash(denyAll: boolean): Builtin {
  return builtin({
    name: "bash",
    description:
      "Run a shell command in the sandbox. Returns exit_code, stdout and stderr previews, and full_output when truncated.",
    input: Input,
    effect: denyAll ? "sandbox_local" : "unguarded",
    run: async ({ command, timeout_ms }, ctx, env) => {
      const session = await sessionOf(env);
      if (!session.ok) return session.error;
      const result = await execute(
        session.value,
        ["sh", "-c", command],
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
}
