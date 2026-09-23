"""HTML to markdown for web_fetch, stdlib only. Keeps what a model reads:
headings, paragraphs, lists, links, code blocks and emphasis. Scripts, styles and markup that
never renders as text are dropped.

ponytail: tables flatten to lines of cells; add a table writer if models need column layout.
"""

import re
from html.parser import HTMLParser
from typing import Final
from urllib.parse import urljoin

_SKIP: Final = frozenset({"script", "style", "noscript", "template", "svg", "head", "iframe"})
_BLOCK: Final = frozenset(
    {"p", "div", "section", "article", "header", "footer", "main", "table", "tr", "ul", "ol"}
    | {"blockquote", "form", "nav", "aside", "figure", "dl", "dt", "dd"}
)
_HEADINGS: Final = {f"h{n}": "#" * n for n in range(1, 7)}
_MARKS: Final = {"strong": "**", "b": "**", "em": "_", "i": "_", "code": "`"}


class _Markdown(HTMLParser):
    def __init__(self, base: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base = base
        self.out: list[str] = []
        self.skip = 0
        self.pre = 0
        self.href: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _SKIP:
            self.skip += 1
        elif tag in _HEADINGS:
            self.out.append(f"\n\n{_HEADINGS[tag]} ")
        elif tag in _BLOCK:
            self.out.append("\n\n")
        elif tag == "br":
            self.out.append("\n")
        elif tag == "li":
            self.out.append("\n- ")
        elif tag in ("td", "th"):
            self.out.append(" | ")
        elif tag == "pre":
            self.pre += 1
            self.out.append("\n\n```\n")
        elif tag == "a":
            href = dict(attrs).get("href")
            self.href.append(urljoin(self.base, href) if href else None)
            self.out.append("[")
        elif tag in _MARKS and not self.pre:
            self.out.append(_MARKS[tag])

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in _HEADINGS or tag in _BLOCK:
            self.out.append("\n\n")
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            self.out.append("\n```\n\n")
        elif tag == "a" and self.href:
            href = self.href.pop()
            self.out.append(f"]({href})" if href else "]")
        elif tag in _MARKS and not self.pre:
            self.out.append(_MARKS[tag])

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        self.out.append(data if self.pre else re.sub(r"\s+", " ", data))


def to_markdown(html: str, base: str) -> str:
    """`base` resolves relative links."""
    parser = _Markdown(base)
    parser.feed(html)
    parser.close()
    text = "".join(parser.out)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
