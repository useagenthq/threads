// The few POSIX commands the fake answers from its in-memory tree, exactly in the argv shapes
// the built-in file tools send (src/tools): `find DIR -type f` and `grep -rnIE -e PAT -- DIR`.
// Anything else is a scripted tool or "command not found".

export type Ran = {
  readonly code: number;
  readonly stdout: string;
  readonly stderr: string;
};

type Tree = ReadonlyMap<string, Uint8Array>;

const text = new TextDecoder("utf-8", { fatal: true });

function under(tree: Tree, dir: string): readonly string[] {
  const prefix = dir.endsWith("/") ? dir : `${dir}/`;
  return [...tree.keys()]
    .filter((p) => p === dir || p.startsWith(prefix))
    .toSorted();
}

function grep(tree: Tree, pattern: string, dir: string): Ran {
  let re: RegExp;
  try {
    re = new RegExp(pattern);
  } catch {
    return { code: 2, stdout: "", stderr: `grep: bad pattern ${pattern}\n` };
  }
  const hits: string[] = [];
  for (const path of under(tree, dir)) {
    let body: string;
    try {
      body = text.decode(tree.get(path));
    } catch {
      continue; // -I: binary files are skipped
    }
    body.split("\n").forEach((line, i) => {
      if (re.test(line)) hits.push(`${path}:${i + 1}:${line}\n`);
    });
  }
  return { code: hits.length > 0 ? 0 : 1, stdout: hits.join(""), stderr: "" };
}

/** The builtin's answer, or undefined when the fake doesn't emulate this argv. */
export function builtin(
  tree: Tree,
  command: readonly string[],
): Ran | undefined {
  const [name, a, b, c, d, e, f] = command;
  if (name === "find" && a !== undefined && b === "-type" && c === "f")
    return {
      code: 0,
      stdout: under(tree, a)
        .map((p) => `${p}\n`)
        .join(""),
      stderr: "",
    };
  if (
    name === "grep" &&
    a === "-rnIE" &&
    b === "-e" &&
    c !== undefined &&
    d === "--" &&
    e !== undefined &&
    f === undefined
  )
    return grep(tree, c, e);
  return undefined;
}

/** The scripted tool a command names: `sh -c "<cmd> ..."` names its first word. */
export function toolName(command: readonly string[]): string {
  const [name = "", flag, script = ""] = command;
  if (name === "sh" && flag === "-c")
    return script.trim().split(/\s+/)[0] ?? "";
  return name;
}
