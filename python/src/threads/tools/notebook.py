"""Jupyter notebooks: `read` shows cells with their outputs, and
`notebook_edit` replaces, inserts or deletes one cell named by its nbformat cell id. Both are
pure functions of the file's bytes; the runner downloads and uploads. An unknown cell id or a
file that isn't a notebook fails before anything is written."""

import json
import uuid
from typing import Final, Literal

from pydantic import JsonValue, TypeAdapter, ValidationError

from threads.result import Err, Ok

type CellType = Literal["code", "markdown", "raw"]
type Mode = Literal["replace", "insert", "delete"]
type Cell = dict[str, JsonValue]

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


def _cell(kind: CellType, source: str) -> Cell:
    cell: Cell = {
        "cell_type": kind,
        "id": uuid.uuid4().hex[:8],
        "metadata": {},
        "source": list[JsonValue](source.splitlines(keepends=True)),
    }
    if kind == "code":
        cell["execution_count"] = None
        cell["outputs"] = []
    return cell


def edit(
    raw: bytes, cell_id: str, source: str, mode: Mode, cell_type: CellType | None = None
) -> Ok[bytes] | Err[str]:
    """The notebook's new bytes. insert adds the new cell after `cell_id`; replace keeps the
    cell's id and clears a code cell's outputs, since they no longer match its source."""
    parsed = _parse(raw)
    if isinstance(parsed, Err):
        return parsed
    notebook, cells = parsed.value
    at = next((i for i, c in enumerate(cells) if c.get("id") == cell_id), None)
    if at is None:
        return Err(f"not_found: no cell with id {cell_id}")
    old = cells[at]
    match mode:
        case "delete":
            cells = [c for i, c in enumerate(cells) if i != at]
        case "insert":
            cells.insert(at + 1, _cell(cell_type or "code", source))
        case "replace":
            kind = cell_type or _kind(old)
            new = {**_cell(kind, source), "id": old.get("id"), "metadata": old.get("metadata", {})}
            cells[at] = new
    notebook["cells"] = list[JsonValue](cells)
    return Ok((json.dumps(notebook, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))


def _kind(cell: Cell) -> CellType:
    match cell.get("cell_type"):
        case "markdown":
            return "markdown"
        case "raw":
            return "raw"
        case _:
            return "code"
