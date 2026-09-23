import { z } from "zod";
import type { EnumOf, Opt, Strict } from "../log/zod-types";

// Inputs of the sandbox tools beyond shell and files: computer use
// against the sandbox desktop, the language servers in the image, and notebook cells.

const Coord: Opt<z.ZodInt> = z.int().min(0).optional();

const COMPUTER_ACTIONS = [
  "click",
  "double_click",
  "right_click",
  "move",
  "drag",
  "scroll",
  "type",
  "key",
] as const;
const DIRECTIONS = ["up", "down", "left", "right"] as const;
const LSP_OPERATIONS = [
  "diagnostics",
  "definition",
  "references",
  "hover",
  "symbols",
] as const;
const CELL_TYPES = ["code", "markdown"] as const;
const NOTEBOOK_MODES = ["replace", "insert", "delete"] as const;

export const ComputerInput: Strict<{
  action: EnumOf<typeof COMPUTER_ACTIONS>;
  x: Opt<z.ZodInt>;
  y: Opt<z.ZodInt>;
  to_x: Opt<z.ZodInt>;
  to_y: Opt<z.ZodInt>;
  text: Opt<z.ZodString>;
  direction: Opt<EnumOf<typeof DIRECTIONS>>;
  amount: Opt<z.ZodInt>;
}> = z.strictObject({
  action: z.enum(COMPUTER_ACTIONS),
  x: Coord.describe(
    "Screenshot pixels: required for click, double_click, right_click, move, drag and scroll.",
  ),
  y: Coord.describe("Screenshot pixels, with x."),
  to_x: Coord.describe("drag: the end point."),
  to_y: Coord.describe("drag: the end point."),
  text: z
    .string()
    .min(1)
    .optional()
    .describe("type: the text. key: a combination such as ctrl+s."),
  direction: z.enum(DIRECTIONS).optional().describe("scroll direction."),
  amount: z
    .int()
    .min(1)
    .max(50)
    .optional()
    .describe("scroll clicks; default 3."),
});

export const ComputerScreenshotInput: Strict<{
  x: Opt<z.ZodInt>;
  y: Opt<z.ZodInt>;
  to_x: Opt<z.ZodInt>;
  to_y: Opt<z.ZodInt>;
  wait_ms: Opt<z.ZodInt>;
}> = z.strictObject({
  x: Coord.describe("Zoom: the region's left edge; give all four or none."),
  y: Coord.describe("Zoom: the region's top edge."),
  to_x: Coord.describe("Zoom: the region's right edge."),
  to_y: Coord.describe("Zoom: the region's bottom edge."),
  wait_ms: z
    .int()
    .min(1)
    .max(60_000)
    .optional()
    .describe("Wait this long before capturing."),
});

export const LspInput: Strict<{
  operation: EnumOf<typeof LSP_OPERATIONS>;
  path: z.ZodString;
  line: Opt<z.ZodInt>;
  character: Opt<z.ZodInt>;
}> = z.strictObject({
  operation: z.enum(LSP_OPERATIONS),
  path: z.string().min(1).describe("Relative to /workspace, or absolute."),
  line: z
    .int()
    .min(1)
    .optional()
    .describe("1-based; required for definition, references and hover."),
  character: z
    .int()
    .min(1)
    .optional()
    .describe("1-based column; required with line."),
});

export const NotebookEditInput: Strict<{
  path: z.ZodString;
  cell_id: z.ZodString;
  new_source: z.ZodString;
  cell_type: Opt<EnumOf<typeof CELL_TYPES>>;
  mode: z.ZodDefault<EnumOf<typeof NOTEBOOK_MODES>>;
}> = z.strictObject({
  path: z.string().min(1).describe("An .ipynb file, relative to /workspace."),
  cell_id: z
    .string()
    .min(1)
    .describe("The nbformat cell id; insert adds the new cell after it."),
  new_source: z.string().describe("The cell's new source; ignored by delete."),
  cell_type: z
    .enum(CELL_TYPES)
    .optional()
    .describe("Required for insert; replace keeps the type when absent."),
  mode: z.enum(NOTEBOOK_MODES).default("replace"),
});
