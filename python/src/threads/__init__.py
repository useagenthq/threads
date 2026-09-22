"""threads: an event-log agent framework."""

from typing import Final

__all__ = ["VERSION", "__version__"]

VERSION: Final[str] = "0.0.0"
"""The package version. Mirrors `VERSION` exported by the TypeScript core."""

__version__: Final[str] = VERSION
