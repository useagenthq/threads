import { existsSync } from "node:fs";
import { join } from "node:path";

/**
 * The interpreter to run spec/tools with.
 *
 * Those generators are 3.12 source. A bare `python3` is whatever the shell hands us, and on a
 * stock macOS that is 3.9, which dies on `type X = ...` with a SyntaxError — so a test that
 * spawns `python3` fails for a reason that has nothing to do with what it is testing.
 * scripts/generate.sh resolves an interpreter the same way; keep the two in step.
 */
export function pythonBin(repoRoot: string): string {
  const venv = join(repoRoot, "python", ".venv", "bin", "python");
  return existsSync(venv) ? venv : "python3";
}
