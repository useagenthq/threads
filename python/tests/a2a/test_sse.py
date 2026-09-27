"""Reading a peer's SSE stream. The WHATWG field rules are pinned by the shared vector
(test_vectors.py); what is pinned here is what the reader does with bytes it cannot read, because a
stream that ends quietly is indistinguishable from one that finished.

Mirrors typescript/packages/a2a/test/sse.test.ts."""

import asyncio
import json
from collections.abc import AsyncIterator, Sequence

from threads.a2a.protocol import MAX_FRAME_BYTES, SseEvent, SseRead, SseRefused, sse_events


def _read(chunks: Sequence[bytes]) -> list[SseRead]:
    async def feed() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    async def collect() -> list[SseRead]:
        return [read async for read in sse_events(feed())]

    return asyncio.run(collect())


class TestACharacterSplitAcrossTwoChunks:
    def test_is_one_character_not_two_replacement_characters(self) -> None:
        # "é" is 0xC3 0xA9, and a real socket splits wherever it likes. Decoding each chunk alone
        # gave two U+FFFD, and U+FFFD is legal inside a JSON string, so the frame still parsed and
        # the corrupted text was handed on as if the peer had sent it.
        reads = _read([b'data: {"t":"\xc3', b'\xa9"}\n\n'])
        assert reads == [SseEvent('{"t":"é"}', None)]
        first = reads[0]
        assert isinstance(first, SseEvent)
        # Worse than a parse failure: U+FFFD is legal in a JSON string, so a lenient decode
        # left a frame that still parsed, carrying text the peer never sent.
        assert json.loads(first.data) == {"t": "é"}

    def test_survives_being_split_one_byte_at_a_time(self) -> None:
        whole = 'data: {"t":"é"}\n\n'.encode()
        reads = _read([bytes([b]) for b in whole])
        assert reads == [SseEvent('{"t":"é"}', None)]


class TestBytesThatAreNotUtf8:
    def test_refuse_rather_than_becoming_a_replacement_character(self) -> None:
        reads = _read([b'data: {"t":"\xff"}\n\n'])
        assert [type(r) for r in reads] == [SseRefused]
        assert isinstance(reads[0], SseRefused)
        assert "UTF-8" in reads[0].why

    def test_a_stream_that_ends_mid_character_refuses(self) -> None:
        reads = _read([b'data: {"t":"\xc3'])
        assert [type(r) for r in reads] == [SseRefused]

    def test_events_before_the_bad_bytes_are_delivered_and_nothing_after(self) -> None:
        reads = _read([b"data: first\n\n", b"data: \xff\n\n", b"data: third\n\n"])
        assert [type(r) for r in reads] == [SseEvent, SseRefused]


class TestTheFrameBudget:
    def test_an_unterminated_line_past_the_budget_refuses(self) -> None:
        # 2 MiB of one line with no break: it used to be accumulated whole and then dropped
        # in silence.
        chunk = b"a" * (256 * 1024)
        reads = _read([chunk] * 8)
        assert [type(r) for r in reads] == [SseRefused]
        assert isinstance(reads[0], SseRefused)
        assert str(MAX_FRAME_BYTES) in reads[0].why

    def test_ordinary_frames_are_not_touched_by_the_budget(self) -> None:
        reads = _read([b"data: a\n\ndata: b\n\ndata: c\n\n"])
        assert [type(r) for r in reads] == [SseEvent, SseEvent, SseEvent]
