import { sha256Hex } from "../hash";
import type { ToolRun } from "../loop/types";
import { err, ok, type Result } from "../result";
import type { SandboxSession } from "../sandbox/protocol";
import {
  type Builtin,
  type BuiltinEnv,
  builtin,
  done,
  failed,
  sessionOf,
} from "./builtin";
import { EditInput, ReadInput, WriteInput } from "./catalog";
import { notebookText } from "./notebook";

// read, write and edit: one POSIX implementation over the session's download and
// upload. Paths are the model's, resolved by the provider under /workspace; permission rules
// saw the same `path` before dispatch.

const utf8 = new TextEncoder();
const strict = new TextDecoder("utf-8", { fatal: true });

async function text(
  env: BuiltinEnv,
  session: SandboxSession,
  path: string,
): Promise<
  Result<{ readonly bytes: Uint8Array; readonly text: string }, ToolRun>
> {
  const got = await session.download(path, env.context);
  if (!got.ok) return err(failed(got.error));
  try {
    return ok({ bytes: got.value, text: strict.decode(got.value) });
  } catch {
    return err(done(`${path} is not UTF-8 text`, true));
  }
}

const numbered = (body: string, from: number, limit: number): string =>
  body
    .split("\n")
    .slice(from - 1, from - 1 + limit)
    .map((line, i) => `${from + i}\t${line}`)
    .join("\n");

export const read: Builtin = builtin({
  name: "read",
  input: ReadInput,
  effect: "read_only",
  run: async ({ path, offset, limit }, _ctx, env) => {
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    const file = await text(env, session.value, path);
    if (!file.ok) return file.error;
    // A notebook reads as its cells with ids and outputs, the ids notebook_edit takes.
    const cells = path.endsWith(".ipynb")
      ? notebookText(file.value.bytes)
      : undefined;
    return done(numbered(cells ?? file.value.text, offset, limit));
  },
});

/** An expected_sha256 that doesn't match: the file is left as it is. */
function mismatch(
  expected: string | undefined,
  bytes: Uint8Array | undefined,
): ToolRun | undefined {
  if (expected === undefined) return undefined;
  const actual = bytes === undefined ? "absent" : sha256Hex(bytes);
  return actual === expected
    ? undefined
    : done(
        `expected_sha256 mismatch: the file is ${actual}; nothing written`,
        true,
      );
}

async function put(
  env: BuiltinEnv,
  session: SandboxSession,
  path: string,
  body: string,
): Promise<ToolRun> {
  const bytes = utf8.encode(body);
  const up = await session.upload(path, bytes, env.context);
  return up.ok
    ? done(`wrote ${bytes.length} bytes to ${path}`)
    : failed(up.error);
}

export const write: Builtin = builtin({
  name: "write",
  input: WriteInput,
  effect: "sandbox_local",
  run: async ({ path, content, expected_sha256 }, _ctx, env) => {
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    if (expected_sha256 !== undefined) {
      const now = await session.value.download(path, env.context);
      if (!now.ok && now.error.code !== "not_found") return failed(now.error);
      const stale = mismatch(expected_sha256, now.ok ? now.value : undefined);
      if (stale !== undefined) return stale;
    }
    return put(env, session.value, path, content);
  },
});

export const edit: Builtin = builtin({
  name: "edit",
  input: EditInput,
  effect: "sandbox_local",
  run: async (input, _ctx, env) => {
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    const file = await text(env, session.value, input.path);
    if (!file.ok) return file.error;
    const stale = mismatch(input.expected_sha256, file.value.bytes);
    if (stale !== undefined) return stale;
    // split/join, not replaceAll: `$&` in new_string is literal text.
    const pieces = file.value.text.split(input.old_string);
    const count = pieces.length - 1;
    if (count === 0) return done("old_string not found; nothing written", true);
    if (count > 1 && input.replace_all !== true)
      return done(`old_string matches ${count} times; nothing written`, true);
    return put(env, session.value, input.path, pieces.join(input.new_string));
  },
});
