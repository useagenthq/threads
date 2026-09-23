"""A hook's redaction lands on the result's first text part, whichever index it has, and its
spans are checked against that part's text (TypeScript's loop/gates.ts textPart)."""

from pydantic import JsonValue

from threads.log import Span, ToolResultData
from threads.reduce.redaction import first_text_part, span_error, text_part

IMAGE: JsonValue = {
    "type": "image_ref",
    "ref": {"sha256": "0" * 64, "bytes": 1, "media_type": "image/png"},
    "width": 1,
    "height": 1,
}


def result(*content: JsonValue) -> ToolResultData:
    data: dict[str, JsonValue] = {
        "call_id": "call_1",
        "completeness": "complete",
        "is_error": False,
        "origin": "executed",
        "preview": "the preview",
    }
    if content:
        data["content"] = list(content)
    return ToolResultData.model_validate(data)


def test_the_first_text_part_is_the_one_after_an_image() -> None:
    parts = result(IMAGE, {"type": "text", "text": "é secret"}, {"type": "text", "text": "tail"})
    assert first_text_part(parts) == 1
    text = text_part(parts, 1)
    assert text == "é secret"
    assert span_error(text, [Span(start=3, end=9)]) is None
    assert span_error(text, [Span(start=1, end=3)]) == "a redaction span splits a character"
    assert span_error(text, [Span(start=3, end=20)]) == "a redaction span lies outside its part"


def test_without_content_the_preview_is_part_zero() -> None:
    assert first_text_part(result()) == 0
    assert text_part(result(), 0) == "the preview"


def test_a_result_with_no_text_part_has_none() -> None:
    assert first_text_part(result(IMAGE)) is None
