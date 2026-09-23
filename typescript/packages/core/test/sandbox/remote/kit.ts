import { expect } from "bun:test";
import { err, ok } from "../../../src/result";
import type {
  ExecOutput,
  Sandbox,
  SandboxContext,
  SandboxSession,
} from "../../../src/sandbox";
import { joined } from "../../../src/sandbox/remote/bytes";
import { unwrap } from "../../store/helpers";
import { CTX } from "../context";

// Shared by the remote kit's tests and each provider adapter's: contexts, and draining exec.

/** A context whose fence refuses, as a stale owner's does; `calls` counts its checks. */
export function staleContext(): SandboxContext & { calls: number } {
  const context = {
    calls: 0,
    authority: CTX.authority,
    fence: async () => {
      context.calls += 1;
      return err({ code: "stale_epoch", message: "lease lost" } as const);
    },
  };
  return context;
}

/** gc's context whose claim was superseded. */
export const LOST_CLAIM: SandboxContext = {
  authority: { kind: "cleanup", resource_id: "r1", claim: "c1" },
  fence: async () =>
    err({ code: "cleanup_claim_lost", message: "claim superseded" }),
};

/** A context that passes once, then refuses: the lease is lost mid-operation. */
export function losesAfter(n: number): SandboxContext {
  let left = n;
  return {
    authority: CTX.authority,
    fence: async () => {
      left -= 1;
      return left >= 0
        ? ok(undefined)
        : err({ code: "stale_epoch", message: "lease lost" });
    },
  };
}

const text = new TextDecoder();

export async function drained(output: ExecOutput): Promise<{
  readonly exit: number;
  readonly stdout: string;
  readonly stderr: string;
}> {
  const [stdout, stderr] = await Promise.all([
    joined(output.stdout),
    joined(output.stderr),
  ]);
  return {
    exit: await output.exit_code,
    stdout: text.decode(stdout),
    stderr: text.decode(stderr),
  };
}

/** Runs a command to completion in the session, under the passing context. */
export async function run(
  session: SandboxSession,
  command: readonly string[],
  options: {
    readonly env?: Record<string, string>;
    readonly stdin?: Uint8Array;
  } = {},
): Promise<Awaited<ReturnType<typeof drained>>> {
  const out = unwrap(
    await session.exec(command, CTX, { processKey: "b:call-1", ...options }),
  );
  return drained(out);
}

/** A fresh sandbox from the adapter; fails the test on an error. */
export async function created(sandbox: Sandbox): Promise<SandboxSession> {
  const made = await sandbox.create("op-1", CTX);
  expect(made.ok).toBe(true);
  return unwrap(made);
}
