"""Every provider factory call in README.md and the provider docs pages builds with today's
options, so a renamed or unknown option in the docs fails here (lane 15B's runnable proof,
until lane 11's example runner executes these blocks)."""

import pathlib
import re
from collections.abc import Callable, Iterator

import pytest

from threads.daytona import daytona
from threads.e2b import e2b
from threads.github import github
from threads.modal import modal
from threads.secrets import secret
from threads.slack import slack
from threads.whatsapp import whatsapp

ROOT = pathlib.Path(__file__).resolve().parents[3]
GUIDES = ROOT / "docs" / "content" / "docs" / "(guides)"
PAGES = (
    ROOT / "README.md",
    *sorted((GUIDES / "sandboxes").glob("*.mdx")),
    *sorted((GUIDES / "host").glob("*.mdx")),
)
FACTORIES: dict[str, Callable[..., object]] = {
    "e2b": e2b,
    "daytona": daytona,
    "modal": modal,
    "slack": slack,
    "github": github,
    "whatsapp": whatsapp,
}
BLOCK = re.compile(r"```python\n(.*?)```", re.DOTALL)
CALL = re.compile(rf"\b({'|'.join(FACTORIES)})\(")


def _call_at(code: str, start: int) -> str:
    """The call expression starting at `start`, up to its balancing parenthesis."""
    depth = 0
    for i in range(code.index("(", start), len(code)):
        depth += {"(": 1, ")": -1}.get(code[i], 0)
        if depth == 0:
            return code[start : i + 1]
    raise ValueError(f"unbalanced call at {code[start : start + 40]!r}")


def calls(text: str) -> Iterator[tuple[str, str]]:
    """(factory, call expression) for each provider factory call in the Python code blocks."""
    for block in BLOCK.findall(text):
        for match in CALL.finditer(block):
            yield match.group(1), _call_at(block, match.start())


def build(expr: str) -> None:
    # Docs text we own, evaluated with only the factories and secret() in scope.
    eval(expr, {"__builtins__": {}, "secret": secret, **FACTORIES})  # noqa: S307


DOCUMENTED = [(page.name, expr) for page in PAGES for _, expr in calls(page.read_text())]


@pytest.mark.parametrize(("page", "expr"), DOCUMENTED)
def test_a_documented_provider_call_builds(page: str, expr: str) -> None:
    build(expr)


def test_every_factory_is_documented_and_an_unknown_option_fails() -> None:
    names = {name for page in PAGES for name, _ in calls(page.read_text())}
    assert names == set(FACTORIES)
    with pytest.raises(TypeError):
        build("e2b(internet=True)")
