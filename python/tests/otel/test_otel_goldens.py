"""Every spec/otel golden, sync by sync: Python sends exactly the bytes TypeScript sends
(typescript/packages/otel/test/goldens.test.ts), at any tick position."""

import pytest
from otel_goldens_kit import Golden, encode, expected, goldens, synced

GOLDENS = goldens()


@pytest.mark.parametrize("g", GOLDENS, ids=[g.case.name for g in GOLDENS])
def test_golden_bytes(g: Golden) -> None:
    for s in g.case.syncs:
        sent = synced(g, s.branch_id, s.cursor_before, s.head_seq)
        assert len(sent) == s.spans
        assert encode(g, sent) == expected(g, s.expected)


@pytest.mark.parametrize("g", GOLDENS, ids=[g.case.name for g in GOLDENS])
def test_every_tick_position_sends_the_same_spans(g: Golden) -> None:
    """A span's bytes depend only on events up to its close: a sync after every append sends
    the same spans, byte for byte, as one sync at the end."""
    for branch in dict.fromkeys(s.branch_id for s in g.case.syncs):
        mine = [s for s in g.case.syncs if s.branch_id == branch]
        start, head = mine[0].cursor_before, mine[-1].head_seq
        once = [encode(g, (s,)) for s in synced(g, branch, start, head)]
        ticked = [
            encode(g, (s,)) for at in range(start, head) for s in synced(g, branch, at, at + 1)
        ]
        assert sorted(b or b"" for b in ticked) == sorted(b or b"" for b in once)
