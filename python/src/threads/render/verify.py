"""Import-time replay of every recorded request (spec/conformance/README.md, `render` step 3).

Requests are checked in seq order, and per request in a pinned order; the first failure wins.
C7 first: its `declared_prefix` equals the line 0 re-rendered from its own settings epoch
(semantic rule 14). Equality, never starts-with, and a mismatch never resets the baseline:
each request is compared with its epoch's pinned settings, not with the previous request.
Then its `request_ref` artifact must exist and verify, and last it must equal Render v1 of
the events before it (rule 15).
"""

from collections.abc import Sequence

from threads.log import Event, ModelRequestEvent, ParseError
from threads.log.digest import sha256_hex
from threads.render.artifacts import ReadArtifact, read_verified
from threads.render.request import epoch_line0, render
from threads.result import Err, Ok


def verify_requests(events: Sequence[Event], read: ReadArtifact) -> Ok[None] | Err[ParseError]:
    for index, event in enumerate(events):
        if isinstance(event, ModelRequestEvent):
            # ponytail: re-renders each request from scratch, O(n^2) over the log; fold the
            # render incrementally if replaying long logs gets slow.
            error = _request_error(events[:index], event, read)
            if error is not None:
                return Err(error)
    return Ok(None)


def _request_error(
    before: Sequence[Event], request: ModelRequestEvent, read: ReadArtifact
) -> ParseError | None:
    head = epoch_line0(before)
    if isinstance(head, Err):
        return head.error
    declared = request.data.declared_prefix
    if (declared.bytes, declared.sha256) != (len(head.value), sha256_hex(head.value)):
        message = "declared_prefix differs from its epoch's line 0"
        return ParseError("prefix_changed", message, request.seq)
    recorded = read_verified(read, request.data.request_ref, request.seq)
    if isinstance(recorded, Err):
        return recorded.error
    rendered = render(before, read, compaction=request.data.purpose == "compaction")
    if isinstance(rendered, Err):
        return rendered.error
    if recorded.value != rendered.value.body:
        message = "request_ref bytes differ from Render v1 of the events before the request"
        return ParseError("request_hash_mismatch", message, request.seq)
    return None
