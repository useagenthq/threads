"""verify_export is the storage trust boundary: invalid bytes in, typed errors out."""

import asyncio
from pathlib import Path

from hypothesis import given, settings
from hypothesis import strategies as st

from threads.log import BranchId
from threads.log.digest import sha256_hex
from threads.log.jcs import canonicalize
from threads.result import Err, Ok
from threads.store import SqliteStore, verify_export
from threads.store.lines import head_line

CASES = Path(__file__).resolve().parents[3] / "spec" / "conformance" / "cases"
SIMPLE = (CASES / "reduce-simple-run" / "log.jsonl").read_bytes()
CHILD = (CASES / "reduce-child-branch" / "log.jsonl").read_bytes()
NOW = 1_790_000_060_000
OTHER = BranchId("0192b000-0000-7000-8000-000000000009")


def lines(log: bytes) -> list[bytes]:
    return log.split(b"\n")[:-1]


def join(parts: list[bytes]) -> bytes:
    return b"".join(part + b"\n" for part in parts)


def error_of(log: bytes) -> tuple[str, int | None]:
    result = verify_export(log, NOW)
    assert isinstance(result, Err), result
    return result.error.code, result.error.seq


def test_the_corpus_logs_verify() -> None:
    for log in (SIMPLE, CHILD):
        result = verify_export(log, NOW)
        assert isinstance(result, Ok)
        assert result.value.head_verified


def test_empty_input_has_no_header() -> None:
    assert error_of(b"") == ("invalid_line", 0)


def test_a_line_that_is_not_utf8_is_invalid() -> None:
    parts = lines(SIMPLE)
    parts[3] = b"\xff" + parts[3]
    assert error_of(join(parts)) == ("invalid_line", 3)


def test_an_event_before_any_header_is_invalid() -> None:
    assert error_of(join(lines(SIMPLE)[1:])) == ("invalid_line", 1)


def test_a_head_before_the_last_line_is_invalid() -> None:
    parts = lines(SIMPLE)
    assert error_of(join([*parts[:3], parts[-1], *parts[3:-1]])) == ("invalid_line", 3)


def test_a_terminated_wrong_head_is_head_mismatch() -> None:
    parts = lines(SIMPLE)
    assert error_of(join([*parts[:-2], parts[-1]])) == ("head_mismatch", 10)


def test_a_child_header_must_be_followed_by_its_fork() -> None:
    parts = lines(CHILD)
    child_header = next(i for i, p in enumerate(parts) if i > 0 and b'"format"' in p)
    assert error_of(join(parts[: child_header + 1])) == ("invalid_transition", 12)
    # seq is checked before the fork link, so a dropped fork is first a seq gap.
    without_fork = [*parts[: child_header + 1], *parts[child_header + 2 :]]
    assert error_of(join(without_fork)) == ("seq_mismatch", 13)


def test_a_fork_event_can_not_continue_a_root_segment() -> None:
    parts = lines(CHILD)
    child_header = next(i for i, p in enumerate(parts) if i > 0 and b'"format"' in p)
    # Drop the child's header: its fork now sits in the root segment (and breaks the chain).
    code, seq = error_of(join([*parts[:child_header], *parts[child_header + 1 :]]))
    assert (code, seq) == ("prev_hash_mismatch", 12)


def test_an_unterminated_chunk_is_a_torn_tail_whatever_it_holds() -> None:
    body = SIMPLE[: SIMPLE.rindex(b"\n", 0, -1) + 1]
    for tail in (SIMPLE[len(body) : -1], b'{"seq":1'):
        result = verify_export(body + tail, NOW)
        assert isinstance(result, Ok)
        assert not result.value.head_verified
        assert result.value.dropped == tail
        assert result.value.committed_bytes == len(body)


@settings(max_examples=300, deadline=None)
@given(st.data())
def test_any_changed_byte_is_detected(data: st.DataObject) -> None:
    """The chain covers every line before the last and the head covers the last, so a changed
    byte either fails verification or leaves the head unverified (a torn final chunk)."""
    index = data.draw(st.integers(0, len(SIMPLE) - 1))
    byte = data.draw(st.integers(0, 255).filter(lambda b: b != SIMPLE[index]))
    changed = SIMPLE[:index] + bytes([byte]) + SIMPLE[index + 1 :]
    result = verify_export(changed, NOW)
    assert isinstance(result, Err) or not result.value.head_verified


def test_another_implementations_branch_is_not_runnable() -> None:
    header = canonicalize(
        {
            "branch_id": OTHER,
            "created_at": NOW,
            "format": "threads.log",
            "format_version": 1,
            "thread_id": "0192a000-0000-7000-8000-000000000001",
            "writer": {"impl": "threads-ts", "version": "0.1.0"},
        }
    )
    assert isinstance(header, Ok)
    raw = header.value.encode()
    log = verify_export(raw + b"\n" + head_line(OTHER, 0, sha256_hex(raw)) + b"\n", NOW)
    assert isinstance(log, Ok)

    async def main() -> None:
        store = await SqliteStore.open()
        try:
            assert await store.import_log(log.value) == Ok(None)
            refused = await store.acquire(OTHER, "py", lambda: NOW)
            assert isinstance(refused, Err)
            assert refused.error.code == "branch_not_runnable"
        finally:
            await store.close()

    asyncio.run(main())
