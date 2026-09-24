"""Test helpers for the tree reader: chunked byte streams and the shared vectors."""

import json
import random
from collections.abc import AsyncIterator, Callable
from pathlib import Path

from pydantic import JsonValue

VECTORS = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "vectors"


def vector(name: str) -> dict[str, JsonValue]:
    loaded: dict[str, JsonValue] = json.loads((VECTORS / name).read_text(encoding="utf-8"))
    return loaded


async def chunked(data: bytes, sizes: Callable[[], int]) -> AsyncIterator[bytes]:
    """`data` in chunks whose sizes come from `sizes`, with an empty chunk now and then."""
    at = 0
    while at < len(data):
        size = sizes()
        if size == 0:
            yield b""
            continue
        yield data[at : at + size]
        at += size


def chunkings(data: bytes, seed: int) -> list[Callable[[], AsyncIterator[bytes]]]:
    """The chunkings every vector is read with: whole, one byte at a time, and random sizes."""
    out: list[Callable[[], AsyncIterator[bytes]]] = [
        lambda: chunked(data, lambda: max(1, len(data))),
        lambda: chunked(data, lambda: 1),
    ]
    for i, most in enumerate((7, 512, 700, 5000)):
        rng = random.Random(seed * 31 + i)  # noqa: S311 - reproducible chunk sizes, not secrets
        out.append(lambda rng=rng, most=most: chunked(data, lambda: rng.randint(0, most)))
    return out
