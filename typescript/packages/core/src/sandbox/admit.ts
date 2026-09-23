import type { ExecOptions } from "./protocol";

// spec/api.json `SandboxSession.exec`: what every session does before its first await. The
// call is copied, so what runs is what was checked even if the caller's objects change
// mid-call, and a caller bug throws before anything reaches the sandbox.

const SHELL_NAME = /^[A-Za-z_][A-Za-z0-9_]*$/;

/** One exec call's inputs, copied from the caller's. */
export type ExecCall = {
  readonly command: readonly string[];
  readonly options: ExecOptions & {
    readonly env: Readonly<Record<string, string>>;
  };
};

/**
 * The call, copied and checked. An empty command, an env name that is not a shell variable
 * name, or one the sandbox-side wrapper keeps for itself (`__t_`), is a caller bug.
 */
export function admitExec(
  command: readonly string[],
  options: ExecOptions,
): ExecCall {
  const { stdin, ...rest } = options;
  const call: ExecCall = {
    command: [...command],
    options: {
      ...rest,
      env: { ...options.env },
      ...(stdin === undefined ? {} : { stdin: stdin.slice() }),
    },
  };
  if (call.command.length === 0) throw new Error("exec needs a command");
  const names = Object.keys(call.options.env).toSorted();
  const bad = names.filter((name) => !SHELL_NAME.test(name));
  if (bad.length > 0)
    throw new Error(`env name is not a shell name: ${bad.join(", ")}`);
  const reserved = names.filter((name) => name.startsWith("__t_"));
  if (reserved.length > 0)
    throw new Error(
      `env names starting __t_ are reserved for the sandbox: ${reserved.join(", ")}`,
    );
  return call;
}
