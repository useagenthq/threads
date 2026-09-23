"""Notebooks: read shows cells with outputs; notebook_edit targets a cell by
its nbformat id and fails before any write on an unknown id or a file that isn't a notebook."""

import json

from pydantic import JsonValue

from threads.result import Err, Ok
from threads.tools.notebook import edit, render

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


def test_replace_keeps_id_and_metadata_and_clears_outputs() -> None:
    got = edit(RAW, "calc", "2 + 2\nprint(4)", "replace")
    assert isinstance(got, Ok)
    assert _cells(got.value)[1] == {
        "cell_type": "code",
        "id": "calc",
        "metadata": {"tags": ["x"]},
        "source": ["2 + 2\n", "print(4)"],
        "execution_count": None,
        "outputs": [],
    }
    assert json.loads(got.value)["metadata"] == META


def test_insert_goes_after_the_cell_and_delete_removes_it() -> None:
    inserted = edit(RAW, "intro", "Some text", "insert", "markdown")
    assert isinstance(inserted, Ok)
    cells = _cells(inserted.value)
    assert [c["cell_type"] for c in cells] == ["markdown", "markdown", "code"]
    assert cells[1]["source"] == ["Some text"]
    assert cells[1]["id"] not in ("intro", "calc")
    deleted = edit(RAW, "intro", "", "delete")
    assert isinstance(deleted, Ok)
    assert [c["id"] for c in _cells(deleted.value)] == ["calc"]


def test_an_unknown_id_fails_before_any_write() -> None:
    assert edit(RAW, "nope", "x", "replace") == Err("not_found: no cell with id nope")
    assert isinstance(edit(b"[]", "calc", "x", "replace"), Err)
