"""Jupyter notebooks: `read` shows cells with their outputs, and
`notebook_edit` replaces, inserts or deletes one cell named by its nbformat cell id. Both are
pure functions of the file's bytes; the runner downloads and uploads. An unknown cell id or a
file that isn't a notebook fails before anything is written."""

import json
from dataclasses import dataclass
from typing import Final, Literal, assert_never

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.result import Err, Ok

type CellType = Literal["code", "markdown"]
type Cell = dict[str, JsonValue]


@dataclass(frozen=True, slots=True)
class Change:
    cell_id: str
    source: str
    mode: Literal["replace", "insert", "delete"]
    cell_type: CellType | None = None


_NOTEBOOK: Final = TypeAdapter(dict[str, JsonValue])
_CELLS: Final = TypeAdapter(list[dict[str, JsonValue]])
OUTPUT_CHARS: Final = 2000
"""Characters of one output shown by read; the file keeps the whole output."""


def _parse(raw: bytes) -> Ok[tuple[dict[str, JsonValue], list[Cell]]] | Err[str]:
    try:
        notebook = _NOTEBOOK.validate_json(raw)
        cells = _CELLS.validate_python(notebook.get("cells"))
    except ValidationError:
        return Err("invalid_path: not a Jupyter notebook (nbformat 4 JSON with cells)")
    return Ok((notebook, cells))


def _text(value: JsonValue) -> str:
    """nbformat multiline strings are a string or a list of strings."""
    if isinstance(value, list):
        return "".join(v for v in value if isinstance(v, str))
    return value if isinstance(value, str) else ""


def _output(output: Cell) -> str:
    kind = output.get("output_type")
    if kind == "stream":
        text = _text(output.get("text"))
    elif kind == "error":
        text = f"{output.get('ename')}: {output.get('evalue')}"
    else:
        data = output.get("data")
        data = data if isinstance(data, dict) else {}
        text = _text(data.get("text/plain"))
        media = sorted(k for k in data if k != "text/plain")
        text += "".join(f"\n[{m} output]" for m in media)
    return text if len(text) <= OUTPUT_CHARS else text[:OUTPUT_CHARS] + "\n[output cut]"


def render(raw: bytes) -> Ok[str] | Err[str]:
    """Each cell with its id, type and source, then its outputs."""
    parsed = _parse(raw)
    if isinstance(parsed, Err):
        return parsed
    blocks: list[str] = []
    for cell in parsed.value[1]:
        blocks.append(f"[cell {cell.get('id', '?')}] {cell.get('cell_type')}")
        blocks.append(_text(cell.get("source")))
        outputs = cell.get("outputs")
        for output in _CELLS.validate_python(outputs) if isinstance(outputs, list) else []:
            blocks.append(f"[output]\n{_output(output)}")
    return Ok("\n".join(blocks) if blocks else "empty notebook")


def _shaped(cell: Cell, kind: CellType, source: str) -> Cell:
    """A code cell carries outputs and an execution count; a markdown cell carries neither.
    Other fields and their order are kept."""
    rest = {k: v for k, v in cell.items() if k not in ("outputs", "execution_count")}
    shaped: Cell = {**rest, "cell_type": kind, "source": source}
    if kind == "code":
        shaped["outputs"] = []
        shaped["execution_count"] = None
    return shaped


def edit(raw: bytes, path: str, change: Change, new_id: str) -> Ok[tuple[bytes, str]] | Err[str]:
    """The notebook's new bytes and what to tell the model, or why nothing is written. insert
    adds the new cell, id `new_id`, after `cell_id`; replace keeps the cell's id."""
    parsed = _parse(raw)
    if isinstance(parsed, Err):
        return Err(f"{path} is not a Jupyter notebook; nothing written")
    notebook, cells = parsed.value
    at = next((i for i, c in enumerate(cells) if c.get("id") == change.cell_id), None)
    if at is None:
        return Err(f"cell_id {change.cell_id} not found; nothing written")
    cell = cells[at]
    match change.mode:
        case "delete":
            cells = [c for i, c in enumerate(cells) if i != at]
            said = f"deleted cell {change.cell_id}"
        case "insert":
            if change.cell_type is None:
                return Err("insert needs cell_type; nothing written")
            blank: Cell = {
                "id": new_id,
                "cell_type": change.cell_type,
                "source": "",
                "metadata": {},
            }
            cells.insert(at + 1, _shaped(blank, change.cell_type, change.source))
            said = f"inserted cell {new_id} after {change.cell_id}"
        case "replace":
            kind = change.cell_type or (
                "markdown" if cell.get("cell_type") == "markdown" else "code"
            )
            same = kind == cell.get("cell_type") and kind != "code"
            cells[at] = (
                {**cell, "source": change.source} if same else _shaped(cell, kind, change.source)
            )
            said = f"replaced cell {change.cell_id}"
        case _:
            assert_never(change.mode)
    notebook["cells"] = list[JsonValue](cells)
    text = json.dumps(notebook, indent=1, ensure_ascii=False) + "\n"
    return Ok((text.encode("utf-8"), said))
