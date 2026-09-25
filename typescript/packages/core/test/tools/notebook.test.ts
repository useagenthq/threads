import { describe, expect, test } from "bun:test";
import { agent, fakeSandbox, scriptedModel, sqlite } from "../../src";
import { openStore } from "../../src/agent/sqlite";
import { knownEvents } from "../../src/reduce";
import { editCells } from "../../src/tools/notebook";
import { NotebookEditInput } from "../../src/tools/sandbox-inputs";
import { unwrap } from "../store/helpers";

// notebook_edit (F1.19): cells are addressed by nbformat id; an unknown id
// fails before anything is written.

const first = { id: "a", cell_type: "markdown", source: "# T", metadata: {} };
const cells = [
  first,
  {
    id: "b",
    cell_type: "code",
    source: ["x = 1\n"],
    metadata: {},
    outputs: [{ output_type: "stream", text: "1" }],
    execution_count: 3,
  },
];
const input = (raw: Record<string, unknown>) =>
  NotebookEditInput.parse({ path: "n.ipynb", new_source: "", ...raw });

describe("editCells", () => {
  test("replace keeps the type and clears a code cell's stale outputs", () => {
    const out = editCells(
      cells,
      input({ cell_id: "b", new_source: "x = 2" }),
      "n",
    );
    expect(out).toEqual({
      cells: [
        first,
        {
          id: "b",
          cell_type: "code",
          source: "x = 2",
          metadata: {},
          outputs: [],
          execution_count: null,
        },
      ],
      said: "replaced cell b",
    });
  });

  test("replace to markdown drops outputs; insert adds after the id; delete removes", () => {
    const md = editCells(
      cells,
      input({ cell_id: "b", new_source: "hi", cell_type: "markdown" }),
      "n",
    );
    expect(typeof md !== "string" && md.cells[1]).toEqual({
      id: "b",
      cell_type: "markdown",
      source: "hi",
      metadata: {},
    });
    const ins = editCells(
      cells,
      input({
        cell_id: "a",
        mode: "insert",
        cell_type: "code",
        new_source: "y",
      }),
      "new1",
    );
    expect(typeof ins !== "string" && ins.cells.map((c) => c.id)).toEqual([
      "a",
      "new1",
      "b",
    ]);
    const del = editCells(cells, input({ cell_id: "a", mode: "delete" }), "n");
    expect(typeof del !== "string" && del.cells.map((c) => c.id)).toEqual([
      "b",
    ]);
  });

  test("an unknown id, or insert without cell_type, changes nothing", () => {
    expect(editCells(cells, input({ cell_id: "zz" }), "n")).toBe(
      "cell_id zz not found; nothing written",
    );
    expect(editCells(cells, input({ cell_id: "a", mode: "insert" }), "n")).toBe(
      "insert needs cell_type; nothing written",
    );
  });
});

describe("notebook_edit in the sandbox", () => {
  test("edits the file by cell id; a missing id and a non-notebook are error results", async () => {
    const usage = { input_tokens: 1, output_tokens: 1 };
    const use = (name: string, args: Record<string, unknown>, id: string) => ({
      content: [{ type: "tool_use", call_id: id, name, input: args }],
      stop_reason: "tool_use",
      usage,
    });
    const nb = JSON.stringify({
      cells,
      metadata: {},
      nbformat: 4,
      nbformat_minor: 5,
    });
    const model = scriptedModel({
      responses: [
        use("write", { path: "n.ipynb", content: nb }, "c1"),
        use(
          "notebook_edit",
          { path: "n.ipynb", cell_id: "a", new_source: "# U" },
          "c2",
        ),
        use(
          "notebook_edit",
          { path: "n.ipynb", cell_id: "q", new_source: "" },
          "c3",
        ),
        use("write", { path: "x.ipynb", content: "not json" }, "c4"),
        use(
          "notebook_edit",
          { path: "x.ipynb", cell_id: "a", new_source: "" },
          "c5",
        ),
        use("read", { path: "n.ipynb" }, "c6"),
        {
          content: [{ type: "text", text: "done" }],
          stop_reason: "end_turn",
          usage,
        },
      ],
    });
    const result = await agent({
      model,
      sandbox: fakeSandbox(),
      permissions: { mode: "bypass", allow_bypass: true },
    }).run("edit", { store: sqlite(":memory:") });
    const { log } = await openStore(result.thread.store);
    const shown = knownEvents(
      unwrap(await log.read(result.thread.branch)),
    ).flatMap((e) =>
      e.type === "tool_result"
        ? [[e.data.is_error, e.data.preview] as const]
        : [],
    );
    expect(shown[1]).toEqual([false, "replaced cell a"]);
    expect(shown[2]).toEqual([true, "cell_id q not found; nothing written"]);
    expect(shown[4]).toEqual([
      true,
      "x.ipynb is not a Jupyter notebook; nothing written",
    ]);
    // read shows the cells by id: cell b kept its source and outputs.
    expect(shown[5]?.[1]).toBe(
      "1\t--- cell a (markdown) ---\n2\t# U\n3\t--- cell b (code) ---\n4\tx = 1\n5\t[out] 1",
    );
  });
});
