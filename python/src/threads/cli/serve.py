"""`threads dev` and `threads start` (spec/api.json cli): load the app's module, find its
host(), and serve `Host.asgi` on uvicorn (the `host` extra). The server's lifespan runs
`ready()` and `stop()`. dev binds localhost and prints each channel's webhook URL to paste into
the provider's settings."""

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

from threads.agents.config import ConfigError
from threads.host import Host


def import_target(target: str) -> ModuleType:
    """`module` or `path/to/file.py`, imported (the module's own errors propagate)."""
    if target.endswith(".py"):
        path = Path(target).resolve()
        found = importlib.util.spec_from_file_location(path.stem, path)
        if found is None or found.loader is None:
            raise ConfigError("invalid_config", f"can't load {target}")
        module = importlib.util.module_from_spec(found)
        sys.modules[path.stem] = module
        found.loader.exec_module(module)
        return module
    sys.path.insert(0, str(Path.cwd()))
    return importlib.import_module(target)


def load(spec: str) -> Host:
    """`module`, `module:attribute`, or `path/to/file.py[:attribute]`. Without an attribute,
    the module's one Host."""
    target, _, attribute = spec.partition(":")
    module = import_target(target)
    if attribute:
        chosen = getattr(module, attribute, None)
        if not isinstance(chosen, Host):
            raise ConfigError("invalid_config", f"{spec} is not a host()")
        return chosen
    hosts = [v for v in vars(module).values() if isinstance(v, Host)]
    if len(hosts) != 1:
        raise ConfigError("invalid_config", f"{target} must define exactly one host()")
    return hosts[0]


def webhook_urls(served: Host, base: str) -> tuple[str, ...]:
    return tuple(f"{base}/channels/{name}/events" for name in served.channels)


def serve(served: Host, host: str, port: int) -> None:
    import uvicorn  # noqa: PLC0415 - the host extra, only when serving

    uvicorn.run(served.asgi, host=host, port=port, lifespan="on", log_level="info")
