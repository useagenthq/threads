"""The global model-request guard: a test process blocks every model but the scripted one, so no
test can reach a real provider (AGENTS.md, Tests)."""

from threads.loop.model import Model
from threads.loop.scripted import ScriptedModel

_blocked = False


def block_model_requests(*, blocked: bool = True) -> None:
    """Called once by a test suite's setup. While blocked, only a scripted model may be sent to."""
    global _blocked  # noqa: PLW0603 - one process-wide switch is the point
    _blocked = blocked


def check(model: Model) -> None:
    """Raises before dispatch when the guard is on and the model is not scripted: a test that
    reaches for a real provider is a bug, never a skipped request."""
    if _blocked and not isinstance(model, ScriptedModel):
        raise RuntimeError(f"model requests are blocked in tests: {model.info.model.name}")
