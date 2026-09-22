"""Verified artifact reads for rendering: a missing or changed artifact is an error, never a
substitute, and it names the seq of the event that carries the reference."""

from collections.abc import Callable, Sequence
from typing import assert_never

from threads.log import (
    ArtifactRef,
    AudioPart,
    AudioRef,
    CitationPart,
    DocumentPart,
    DocumentRef,
    HostedToolPart,
    ImagePart,
    ImageRef,
    ParseError,
    ReasoningPart,
    TextPart,
    ToolUsePart,
)
from threads.log.digest import sha256_hex
from threads.result import Err, Ok

type ReadArtifact = Callable[[str], Ok[bytes] | Err[ParseError]]
"""Artifact bytes by sha256, e.g. `ArtifactStore.get`."""
type AnyRef = ArtifactRef | ImageRef | DocumentRef | AudioRef
type Part = (
    TextPart
    | ToolUsePart
    | ImagePart
    | DocumentPart
    | AudioPart
    | ReasoningPart
    | HostedToolPart
    | CitationPart
)


def read_verified(read: ReadArtifact, ref: AnyRef, seq: int) -> Ok[bytes] | Err[ParseError]:
    """The artifact's bytes, checked against the ref's sha256 and length whatever the store
    already checked: artifacts come back across a trust boundary."""
    match read(ref.sha256):
        case Err(error=error):
            return Err(ParseError(error.code, error.message, seq))
        case Ok(value=data):
            if len(data) != ref.bytes or sha256_hex(data) != ref.sha256:
                message = f"artifact {ref.sha256} fails its hash or length"
                return Err(ParseError("artifact_corrupt", message, seq))
            return Ok(data)


def read_text(read: ReadArtifact, ref: AnyRef, seq: int) -> Ok[str] | Err[ParseError]:
    """A verified artifact that holds text, decoded as UTF-8."""
    got = read_verified(read, ref, seq)
    if isinstance(got, Err):
        return got
    try:
        return Ok(got.value.decode("utf-8"))
    except UnicodeDecodeError:
        return Err(ParseError("artifact_corrupt", f"artifact {ref.sha256} is not UTF-8", seq))


def part_refs(parts: Sequence[Part]) -> tuple[AnyRef, ...]:
    """The artifacts the rendered parts reference, in part order."""
    refs: list[AnyRef] = []
    for part in parts:
        match part:
            case TextPart() | ToolUsePart():
                pass
            case ImagePart() | DocumentPart() | AudioPart() | ReasoningPart() | HostedToolPart():
                refs.append(part.ref)
            case CitationPart():
                if isinstance(part.ref, ArtifactRef):
                    refs.append(part.ref)
            case _:
                assert_never(part)
    return tuple(refs)
