"""Hydration (design §2.4): a result API turns a stored result into a MemberResult, reading a
completed output's {ref} from the content-addressed store, verified by sha256 and length. A ref
the verified log names is always present in a correct store, so a failure raises
StoreCorruptError."""

from collections.abc import Awaitable, Callable
from typing import Literal, assert_never

from pydantic.experimental.missing_sentinel import MISSING

from threads.agents.team_answers import (
    MemberBudgetExhausted,
    MemberCancelled,
    MemberCompleted,
    MemberError,
    MemberFailed,
    MemberHandedOff,
    MemberResult,
)
from threads.log import (
    ArtifactRef,
    BudgetExceededData,
    CancelledResult,
    CompletedResult,
    ExhaustedResult,
    FailedResult,
    HandedOffResult,
    ParseError,
    StoredMemberResult,
)
from threads.reduce.handlers import to_json
from threads.result import Err, Ok


class StoreCorruptError(Exception):
    """Raised by the result reads (Team.members, wait, ask and ask_status) when a result the
    verified log names is missing or corrupt in the artifact store: a broken store invariant,
    never an expected failure."""

    def __init__(
        self, code: Literal["artifact_missing", "artifact_corrupt"], ref: ArtifactRef, message: str
    ) -> None:
        super().__init__(f"{code}: {message}")
        self.code: Literal["artifact_missing", "artifact_corrupt"] = code
        self.ref = ref
        """The verified ref that failed."""


type ReadArtifact = Callable[[str], Awaitable[Ok[bytes] | Err[ParseError]]]


async def hydrated(stored: StoredMemberResult, read: ReadArtifact) -> MemberResult:
    """The public result of a stored one. Raises StoreCorruptError."""
    match stored:
        case CompletedResult():
            body = stored.output
            text = body.text if body.text is not MISSING else None
            if text is None:
                if body.ref is MISSING:
                    raise AssertionError("a text body is text or a ref")
                text = await read_text(read, body.ref)
            return MemberCompleted(stored.member, text)
        case FailedResult():
            error = MemberError(stored.error.code, stored.error.message)
            return MemberFailed(stored.member, error)
        case CancelledResult():
            return MemberCancelled(stored.member)
        case ExhaustedResult():
            budget = BudgetExceededData.model_validate(to_json(stored.budget))
            return MemberBudgetExhausted(stored.member, budget)
        case HandedOffResult():
            return MemberHandedOff(stored.member, stored.to_thread)
        case _:
            assert_never(stored)


async def read_text(read: ReadArtifact, ref: ArtifactRef) -> str:
    got = await read(ref.sha256)
    if isinstance(got, Err):
        missing = got.error.code == "artifact_missing"
        code = "artifact_missing" if missing else "artifact_corrupt"
        raise StoreCorruptError(code, ref, got.error.message)
    if len(got.value) != ref.bytes:
        message = f"artifact {ref.sha256} is {len(got.value)} bytes, not {ref.bytes}"
        raise StoreCorruptError("artifact_corrupt", ref, message)
    try:
        return got.value.decode()
    except UnicodeDecodeError as e:
        raise StoreCorruptError("artifact_corrupt", ref, str(e)) from e
