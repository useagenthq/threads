import { z } from "zod";
import { assertNever } from "../assert-never";
import { sha256Hex } from "../hash";
import type { Opt } from "../log/zod-types";
import type { ToolRun } from "../loop/types";
import { type Builtin, builtin, done, failed, sessionOf } from "./builtin";
import { NotebookEditInput } from "./sandbox-inputs";

// notebook_edit: one cell of an nbformat 4 notebook, by its cell id, through
// the session's download and upload. A missing id, or a file that isn't a notebook, fails
// before anything is written.

type Input = z.infer<typeof NotebookEditInput>;

// The file is sandbox output: parsed at the boundary. Loose, so fields we don't edit survive.
type Loose<S extends z.core.$ZodLooseShape> = z.ZodObject<S, z.core.$loose>;
const Cell: Loose<{
  id: Opt<z.ZodString>;
  cell_type: z.ZodString;
  source: z.ZodUnion<[z.ZodString, z.ZodArray<z.ZodString>]>;
}> = z.looseObject({
  id: z.string().optional(),
  cell_type: z.string(),
  source: z.union([z.string(), z.array(z.string())]),
});
const Notebook: Loose<{ cells: z.ZodArray<typeof Cell> }> = z.looseObject({
  cells: z.array(Cell),
});
type Cell = z.infer<typeof Cell>;

const utf8 = new TextEncoder();
const strict = new TextDecoder("utf-8", { fatal: true });

function parse(bytes: Uint8Array): z.infer<typeof Notebook> | undefined {
  try {
    const parsed = Notebook.safeParse(JSON.parse(strict.decode(bytes)));
    return parsed.success ? parsed.data : undefined;
  } catch {
    return undefined;
  }
}

/** A code cell carries outputs and an execution count; a markdown cell carries neither. */
function shaped(cell: Cell, type: "code" | "markdown", source: string): Cell {
  const { outputs: _o, execution_count: _e, ...rest } = cell;
  return type === "code"
    ? { ...rest, cell_type: type, source, outputs: [], execution_count: null }
    : { ...rest, cell_type: type, source };
}

/** The edited cells and what to tell the model, or the reason nothing changes. */
export function editCells(
  cells: readonly Cell[],
  input: Input,
  newId: string,
): { readonly cells: readonly Cell[]; readonly said: string } | string {
  const at = cells.findIndex((c) => c.id === input.cell_id);
  if (at === -1) return `cell_id ${input.cell_id} not found; nothing written`;
  const cell = cells[at];
  if (cell === undefined) throw new Error("findIndex returned a cell");
  switch (input.mode) {
    case "delete":
      return { cells: cells.toSpliced(at, 1), said: `deleted cell ${cell.id}` };
    case "insert": {
      if (input.cell_type === undefined)
        return "insert needs cell_type; nothing written";
      const added = shaped(
        { id: newId, cell_type: input.cell_type, source: "", metadata: {} },
        input.cell_type,
        input.new_source,
      );
      return {
        cells: cells.toSpliced(at + 1, 0, added),
        said: `inserted cell ${newId} after ${cell.id}`,
      };
    }
    case "replace": {
      const type =
        input.cell_type ??
        (cell.cell_type === "markdown" ? "markdown" : "code");
      const replaced =
        type === cell.cell_type && type !== "code"
          ? { ...cell, source: input.new_source }
          : shaped(cell, type, input.new_source);
      return {
        cells: cells.with(at, replaced),
        said: `replaced cell ${cell.id}`,
      };
    }
    default:
      return assertNever(input.mode);
  }
}

export const notebookEdit: Builtin = builtin({
  name: "notebook_edit",
  input: NotebookEditInput,
  effect: "sandbox_local",
  run: async (input, ctx, env): Promise<ToolRun> => {
    const session = await sessionOf(env);
    if (!session.ok) return session.error;
    const got = await session.value.download(input.path, env.context);
    if (!got.ok) return failed(got.error);
    const notebook = parse(got.value);
    if (notebook === undefined)
      return done(
        `${input.path} is not a Jupyter notebook; nothing written`,
        true,
      );
    // Deterministic, so a replayed or reconciled call names the same cell.
    const edited = editCells(
      notebook.cells,
      input,
      sha256Hex(utf8.encode(ctx.effectKey)).slice(0, 16),
    );
    if (typeof edited === "string") return done(edited, true);
    const body = `${JSON.stringify({ ...notebook, cells: edited.cells }, null, 1)}\n`;
    const up = await session.value.upload(
      input.path,
      utf8.encode(body),
      env.context,
    );
    return up.ok ? done(edited.said) : failed(up.error);
  },
});
