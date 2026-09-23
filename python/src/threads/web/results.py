"""web_fetch and web_search results. Both are untrusted reference: the page or
hit text is a text part annotated by a `citation{source_kind: web}` part, and the fetched bytes
are an artifact the citation names, so replay reads exactly what the model saw."""

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Final

from threads.log import ArtifactRef, CitationPart, ResultPart, TextPart
from threads.loop.tools import Dispatched, NotSent, Output, Uncertain
from threads.redaction import contains_secret, redact_bytes
from threads.web.fetch import Moved, Page
from threads.web.http import WebError
from threads.web.markdown import to_markdown
from threads.web.search import SearchError, SearchHit

type Put = Callable[[bytes, str], Awaitable[ArtifactRef]]
"""Stores bytes as a durable artifact of that media type and returns its ref."""

TEXT_CAP: Final = 100_000
"""Characters of converted content shown to the model; the whole page stays in the artifact."""
_TEXTUAL: Final = ("text/", "application/json", "application/xml", "application/xhtml+xml")


def _media(kind: str) -> str:
    """The artifact's media type: the response's when it is well formed."""
    ok = kind.count("/") == 1 and all(c.isalnum() or c in "./+-" for c in kind)
    return kind if ok else "application/octet-stream"


async def page_output(page: Page, put: Put) -> Output:
    textual = page.media_type.startswith(_TEXTUAL)
    if not textual and contains_secret(page.body):
        # Non-text bytes are never edited, so a page holding a secret is not stored (C5).
        return Output(f"URL: {page.url}\nthe response holds a registered secret; not kept", True)
    # The page is stored as the cited artifact: redacted first (C5).
    body = redact_bytes(page.body) if textual else page.body
    if contains_secret(body):
        # An escaped form redaction can't replace (a JSON page) is refused, never stored.
        return Output(f"URL: {page.url}\nthe response holds a registered secret; not kept", True)
    page = replace(page, body=body)
    ref = await put(page.body, _media(page.media_type))
    header = f"URL: {page.url}\nStatus: {page.status}\nSHA-256: {ref.sha256}\n"
    if page.truncated:
        header += "The response was cut at the size cap.\n"
    if not textual:
        body = f"{page.media_type} content ({len(page.body)} bytes), not shown as text."
    else:
        text = _decode(page.body, page.charset)
        html = page.media_type in ("text/html", "application/xhtml+xml")
        body = to_markdown(text, page.url) if html else text
        if len(body) > TEXT_CAP:
            body = body[:TEXT_CAP] + "\n[content cut at the cap; the full page is the artifact]"
    text = f"{header}\n{body}"
    cite = CitationPart(type="citation", source_kind="web", source_id=page.url, ref=ref)
    parts: tuple[ResultPart, ...] = (TextPart(type="text", text=text), cite)
    return Output(text, page.status >= 400, content=parts)  # noqa: PLR2004 - HTTP errors


def _decode(body: bytes, charset: str | None) -> str:
    try:
        return body.decode(charset or "utf-8", "replace")
    except LookupError:
        return body.decode("utf-8", "replace")


def moved_output(moved: Moved) -> Output:
    return Output(
        f"The page redirects to another host: {moved.url}\n"
        "Call web_fetch with that URL to follow it."
    )


def failed(error: str | WebError | SearchError) -> Dispatched:
    """A refusal is an error result, and so is a failed read: web tools are read_only, so the
    loop records an uncertain read as an error result. A refused fence sent nothing."""
    if isinstance(error, str):
        return Output(error, True)
    if error.code == "stale_epoch":
        return NotSent()
    if error.code == "timeout":
        return Uncertain("timeout")
    if isinstance(error, WebError) and error.sent:
        return Uncertain("transport_error")
    return Output(f"{error.code}: {error.message}", True)


def hits_output(query: str, hits: Sequence[SearchHit]) -> Output:
    if not hits:
        return Output(f"No results for {query!r}.")
    parts: list[ResultPart] = []
    lines: list[str] = []
    for n, hit in enumerate(hits, 1):
        line = f"{n}. {hit.title}\n   {hit.url}\n   {hit.snippet}".rstrip()
        lines.append(line)
        parts.append(TextPart(type="text", text=line))
        parts.append(
            CitationPart(type="citation", source_kind="web", source_id=hit.url, title=hit.title)
        )
    return Output("\n".join(lines), content=tuple(parts))
