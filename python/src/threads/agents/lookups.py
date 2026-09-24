"""Setup's check of declared recovery lookups (spec/api.json `Model.lookup`, `Sandbox.lookup`,
`Sandbox.lookupSnapshot`): an adapter that declares a lookup capability implements the method,
so recovery never meets a declared lookup it can't ask. Run by check() and the first run."""

from collections.abc import Iterable

from threads.agents.config import ConfigError
from threads.loop.model import Model
from threads.sandbox.protocol import Sandbox


def check_lookups(models: Iterable[Model], sandbox: Sandbox | None) -> None:
    """Raises ConfigError capability_missing, naming the adapter and the missing method."""
    for model in models:
        declared = model.info.lookup
        if declared != "none" and not _has(model, "lookup"):
            ref = model.info.model
            raise ConfigError(
                "capability_missing",
                f"model {ref.provider}/{ref.name} declares lookup {declared!r} but has no "
                "lookup method: implement lookup, or declare lookup='none'",
            )
    if sandbox is None:
        return
    info = sandbox.info
    if info.lookup.create != "none" and not _has(sandbox, "lookup"):
        raise _missing(info.provider, "create", info.lookup.create, "lookup")
    if info.lookup.snapshot != "none" and not _has(sandbox, "lookup_snapshot"):
        raise _missing(info.provider, "snapshot", info.lookup.snapshot, "lookup_snapshot")


def _has(adapter: object, method: str) -> bool:
    """A callable method, not just an attribute: the runtime-checkable capability protocols
    only check that the name exists, so a dataclass field named `lookup` would pass them."""
    return callable(getattr(adapter, method, None))


def _missing(provider: str, operation: str, declared: str, method: str) -> ConfigError:
    return ConfigError(
        "capability_missing",
        f"sandbox {provider} declares lookup.{operation} {declared!r} but has no {method} "
        f"method: implement {method}, or declare lookup.{operation}='none'",
    )
