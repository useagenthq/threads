"""The lsp tool: read_only, run in the sandbox against the image's language
servers. Each call uploads the driver (`lsp_driver.py`), which starts the server, asks one
question and exits. A server that is missing or doesn't answer is `unavailable`, never an empty
success.

ponytail: a server per call; keep one running per language if indexing time hurts.
"""

import json
import posixpath
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import JsonValue, TypeAdapter, ValidationError
from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import LspInput
from threads.loop.tools import Dispatched, NotSent, Output
from threads.result import Err
from threads.tools import files
from threads.tools.control import Control

LANGUAGES: Final[Mapping[str, tuple[str, ...]]] = {
    "python": ("pyright-langserver", "--stdio"),
    "typescript": ("typescript-language-server", "--stdio"),
    "go": ("gopls",),
    "rust": ("rust-analyzer",),
}
"""Each declarable language and the server command its image must have."""
_IDS: Final = {
    ".py": ("python", "python"),
    ".pyi": ("python", "python"),
    ".ts": ("typescript", "typescript"),
    ".tsx": ("typescript", "typescriptreact"),
    ".js": ("typescript", "javascript"),
    ".jsx": ("typescript", "javascriptreact"),
    ".mjs": ("typescript", "javascript"),
    ".cjs": ("typescript", "javascript"),
    ".go": ("go", "go"),
    ".rs": ("rust", "rust"),
}
"""A file extension's declared language and its LSP languageId."""
DRIVER: Final = "/workspace/.threads/lsp_driver.py"
_TIMEOUT_MS: Final = 120_000
_ANSWER: Final = TypeAdapter(dict[str, JsonValue])
_SEVERITY: Final = {1: "error", 2: "warning", 3: "info", 4: "hint"}


async def run(  # noqa: PLR0911 - one early answer per failure
    tools: Control,
    args: LspInput,
    key: str,
    servers: Mapping[str, Sequence[str]],
) -> Dispatched:
    """`servers`: the declared languages' server commands."""
    path = files.absolute(args.path)
    language, language_id = _IDS.get(posixpath.splitext(path)[1], ("", ""))
    if language not in servers:
        return Output(f"unavailable: no language server is declared for {args.path}", True)
    positional = args.operation in ("definition", "references", "hover")
    if positional and (args.line is MISSING or args.character is MISSING):
        return Output(f"{args.operation} needs line and character", True)
    session = await tools.session()
    if session is None:
        return NotSent()
    driver = (Path(__file__).parent / "lsp_driver.py").read_bytes()
    up = await session.upload(DRIVER, driver, tools.context)
    if isinstance(up, Err):
        return Output(f"unavailable: {up.error.code}: {up.error.message}", True)
    argv = [
        "python3",
        DRIVER,
        json.dumps(list(servers[language])),
        "/workspace",
        path,
        language_id,
        args.operation,
    ]
    if args.line is not MISSING and args.character is not MISSING:
        argv += [str(args.line), str(args.character)]
    ran = await tools.command(argv, key, _TIMEOUT_MS)
    if isinstance(ran, Err):
        return ran.error
    try:
        answer = _ANSWER.validate_json(ran.value.stdout.strip().rsplit("\n", 1)[-1])
    except ValidationError:
        return Output(f"unavailable: the lsp driver failed: {ran.value.stderr.strip()}", True)
    if "unavailable" in answer or answer.get("result") is None:
        why = answer.get("unavailable") or f"the server gave no {args.operation}"
        return Output(f"unavailable: {why}", True)
    return Output(render(args.operation, answer["result"]))


def _start(span: JsonValue) -> str:
    """A range's start as line:character, 1-based, or empty."""
    start = span.get("start") if isinstance(span, dict) else None
    line = start.get("line") if isinstance(start, dict) else None
    char = start.get("character") if isinstance(start, dict) else None
    if isinstance(line, int) and isinstance(char, int):
        return f"{line + 1}:{char + 1}"
    return ""


def _where(value: JsonValue) -> str:
    """A Location or LocationLink as path:line:character."""
    if not isinstance(value, dict):
        return str(value)
    uri = str(value.get("uri", value.get("targetUri"))).removeprefix("file://")
    at = _start(value.get("range", value.get("targetSelectionRange")))
    return f"{uri}:{at}" if at else uri


def _hover(contents: JsonValue) -> str:
    if isinstance(contents, list):
        return "\n\n".join(_hover(c) for c in contents)
    if isinstance(contents, dict):
        return str(contents.get("value", ""))
    return str(contents)


def _symbols(items: Sequence[JsonValue], depth: int = 0) -> list[str]:
    """DocumentSymbol trees or SymbolInformation lists."""
    out: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        location = item.get("location")
        span = location.get("range") if isinstance(location, dict) else item.get("range")
        at = _start(span) or "?"
        out.append(f"{'  ' * depth}{item.get('name')} (kind {item.get('kind')}) at {at}")
        children = item.get("children")
        if isinstance(children, list):
            out += _symbols(children, depth + 1)
    return out


def _diagnostic(item: JsonValue) -> str:
    if not isinstance(item, dict):
        return str(item)
    severity = item.get("severity")
    label = _SEVERITY.get(severity, "diagnostic") if isinstance(severity, int) else "diagnostic"
    return f"{_start(item.get('range'))} {label}: {item.get('message')}"


def render(operation: str, result: JsonValue) -> str:
    """The server's answer as text, positions 1-based."""
    items: Sequence[JsonValue] = result if isinstance(result, list) else [result]
    match operation:
        case "diagnostics":
            return "\n".join(_diagnostic(d) for d in items) or "no diagnostics"
        case "definition" | "references":
            return "\n".join(_where(i) for i in items) or f"no {operation}"
        case "hover":
            text = _hover(result.get("contents") if isinstance(result, dict) else result)
            return text or "no hover information"
        case _:
            return "\n".join(_symbols(items)) or "no symbols"
