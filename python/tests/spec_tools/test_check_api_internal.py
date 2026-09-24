"""check_api.py refuses user-facing text that names an internal entry (spec/tools/api_internal.py):
the telemetry feed is `@threads/core/internal/feed` / `threads.store._feed`, never documented."""

import pathlib

from api_internal import check_internal_mentions

ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_the_repository_names_no_internal_entry() -> None:
    assert check_internal_mentions(ROOT) == []


def test_a_docs_page_that_mentions_the_internal_feed_fails(tmp_path: pathlib.Path) -> None:
    page = tmp_path / "docs" / "content" / "docs" / "telemetry.mdx"
    page.parent.mkdir(parents=True)
    page.write_text('import { Feed } from "@threads/core/internal/feed";\n')
    example = tmp_path / "python" / "examples" / "feed.py"
    example.parent.mkdir(parents=True)
    example.write_text("from threads.store._feed import Feed\n")
    problems = check_internal_mentions(tmp_path)
    assert problems == [
        "docs/content/docs/telemetry.mdx: mentions @threads/core/internal/feed, an internal"
        " entry that is never documented",
        "python/examples/feed.py: mentions threads.store._feed, an internal entry that is never"
        " documented",
    ]


def test_public_entries_are_fine(tmp_path: pathlib.Path) -> None:
    page = tmp_path / "README.md"
    page.write_text('import { otel } from "@threads/otel";\nfrom threads.otel import otel\n')
    assert check_internal_mentions(tmp_path) == []
