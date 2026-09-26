"""The global model-request guard: a test process blocks every model but the scripted one, so no
test can reach a real provider (AGENTS.md, Tests)."""

from threads.loop.model import Model
from threads.loop.scripted import ScriptedModel

_blocked = False
_seen = [0]
"""Requests to a model that isn't the scripted test kit, blocked or not, in this process."""


def requests_seen() -> int:
    """How many requests this process has tried to send to a real model: a test proves an
    offline eval adds none."""
    return _seen[0]


class ModelBlockedError(RuntimeError):
    """Raised by the test model-request guard before dispatch (spec/api.json ModelBlockedError,
    one type name in both languages). A RuntimeError, so existing `except RuntimeError` blocks
    behave the same; the eval runner catches exactly this type."""

    def __init__(self, model: str) -> None:
        super().__init__(f"model requests are blocked in tests: {model}")
        self.model = model
        """provider/name of the blocked model."""


def block_model_requests(*, blocked: bool = True) -> None:
    """Called once by a test suite's setup. While blocked, only a scripted model may be sent to."""
    global _blocked  # noqa: PLW0603 - one process-wide switch is the point
    _blocked = blocked


def blocked_model(model: Model) -> str | None:
    """The model as the block would name it, or None when it is allowed. Counts no request, so a
    caller can refuse a model before it ever runs (the eval runner's simulated user)."""
    if isinstance(model, ScriptedModel) or not _blocked:
        return None
    ref = model.info.model
    return f"{ref.provider}/{ref.name}"


def check(model: Model) -> None:
    """Raises before dispatch when the guard is on and the model is not scripted: a test that
    reaches for a real provider is a bug, never a skipped request."""
    if isinstance(model, ScriptedModel):
        return
    _seen[0] += 1
    named = blocked_model(model)
    if named is not None:
        raise ModelBlockedError(named)
