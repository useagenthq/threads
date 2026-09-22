"""Import-time replay of every recorded request (spec/conformance/README.md, `render` steps 2-3).

C7 first: every `declared_prefix` equals the line 0 re-rendered from its own settings epoch
(semantic rule 14). Equality, never starts-with, and a mismatch never resets the baseline:
each request is compared with its epoch's pinned settings, not with the previous request.
Then every request re-renders from the events before it to its `request_ref` bytes (rule 15).
"""

from collections.abc import Sequence

from threads.log import Event, ModelRequestEvent, ParseError
from threads.log.digest import sha256_hex
from threads.render.artifacts import ReadArtifact, read_verified
from threads.render.request import epoch_line0, render
from threads.result import Err, Ok


def verify_requests(events: Sequence[Event], read: ReadArtifact) -> Ok[None] | Err[ParseError]:
    requests = [(i, e) for i, e in enumerate(events) if isinstance(e, ModelRequestEvent)]
    for index, request in requests:
        error = _prefix_error(events[:index], request)
        if error is not None:
            return Err(error)
    for index, request in requests:
        # ponytail: re-renders each request from scratch, O(n^2) over the log; fold the render
        # incrementally if replaying long logs gets slow.
        error = _request_error(events[:index], request, read)
        if error is not None:
            return Err(error)
    return Ok(None)


def _prefix_error(before: Sequence[Event], request: ModelRequestEvent) -> ParseError | None:
    head = epoch_line0(before)
    if isinstance(head, Err):
        return head.error
    declared = request.data.declared_prefix
    if (declared.bytes, declared.sha256) != (len(head.value), sha256_hex(head.value)):
        return ParseError(
            "prefix_changed", "declared_prefix differs from its epoch's line 0", request.seq
        )
    return None


def _request_error(
    before: Sequence[Event], request: ModelRequestEvent, read: ReadArtifact
) -> ParseError | None:
    rendered = render(before, read, compaction=request.data.purpose == "compaction")
    if isinstance(rendered, Err):
        return rendered.error
    recorded = read_verified(read, request.data.request_ref, request.seq)
    if isinstance(recorded, Err):
        return recorded.error
    if recorded.value != rendered.value.body:
        message = "request_ref bytes differ from Render v1 of the events before the request"
        return ParseError("request_hash_mismatch", message, request.seq)
    return None
