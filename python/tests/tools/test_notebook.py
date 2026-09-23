"""Notebooks: read shows cells with outputs; notebook_edit targets a cell by
its nbformat id and fails before any write on an unknown id or a file that isn't a notebook."""

import json

from pydantic import JsonValue

from threads.result import Err, Ok
from threads.tools.notebook import Change, edit, render

META: dict[str, JsonValue] = {"kernelspec": {"name": "python3"}}
NB: dict[str, JsonValue] = {
    "nbformat": 4,
    "nbformat_minor": 5,
    "metadata": META,
    "cells": [
        {"cell_type": "markdown", "id": "intro", "metadata": {}, "source": ["# Title\n"]},
        {
            "cell_type": "code",
            "id": "calc",
            "metadata": {"tags": ["x"]},
            "execution_count": 1,
            "source": "1 + 1",
            "outputs": [
                {"output_type": "execute_result", "data": {"text/plain": "2", "image/png": "iV"}},
                {"output_type": "stream", "name": "stdout", "text": ["hi\n"]},
                {"output_type": "error", "ename": "E", "evalue": "boom", "traceback": []},
            ],
        },
    ],
}
RAW = json.dumps(NB).encode()


def _cells(raw: bytes) -> list[dict[str, JsonValue]]:
    return json.loads(raw)["cells"]


def test_render_shows_cells_and_outputs() -> None:
    assert render(RAW) == Ok(
        "[cell intro] markdown\n# Title\n\n[cell calc] code\n1 + 1\n"
        "[output]\n2\n[image/png output]\n[output]\nhi\n\n[output]\nE: boom"
    )
    assert isinstance(render(b"not json"), Err)
    assert isinstance(render(b'{"cells": 3}'), Err)


def _edit(change: Change) -> tuple[bytes, str]:
    got = edit(RAW, "a.ipynb", change, "newid")
    assert isinstance(got, Ok)
    return got.value


def test_replace_keeps_id_type_and_metadata_and_clears_outputs() -> None:
    raw, said = _edit(Change("calc", "2 + 2", "replace"))
    assert said == "replaced cell calc"
    assert _cells(raw)[1] == {
        "cell_type": "code",
        "id": "calc",
        "metadata": {"tags": ["x"]},
        "source": "2 + 2",
        "outputs": [],
        "execution_count": None,
    }
    assert json.loads(raw)["metadata"] == META
    # Same non-code type: only the source changes.
    raw, _ = _edit(Change("intro", "# New", "replace"))
    assert _cells(raw)[0] == {
        "cell_type": "markdown",
        "id": "intro",
        "metadata": {},
        "source": "# New",
    }


def test_insert_goes_after_the_cell_and_delete_removes_it() -> None:
    raw, said = _edit(Change("intro", "x = 1", "insert", "code"))
    assert said == "inserted cell newid after intro"
    cells = _cells(raw)
    assert [c["id"] for c in cells] == ["intro", "newid", "calc"]
    assert cells[1] == {
        "id": "newid",
        "cell_type": "code",
        "source": "x = 1",
        "metadata": {},
        "outputs": [],
        "execution_count": None,
    }
    raw, said = _edit(Change("intro", "", "delete"))
    assert said == "deleted cell intro"
    assert [c["id"] for c in _cells(raw)] == ["calc"]


def test_bad_edits_write_nothing() -> None:
    assert edit(RAW, "a.ipynb", Change("nope", "x", "replace"), "n") == Err(
        "cell_id nope not found; nothing written"
    )
    assert edit(RAW, "a.ipynb", Change("intro", "x", "insert"), "n") == Err(
        "insert needs cell_type; nothing written"
    )
    assert edit(b"[]", "b.ipynb", Change("calc", "x", "replace"), "n") == Err(
        "b.ipynb is not a Jupyter notebook; nothing written"
    )
