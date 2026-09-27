"""The A2A data model under the protobuf JSON mapping. Every rule here is one the proto states and
JSON Schema can express, so it is enforced where a peer's bytes arrive rather than trusted: both
languages read the one exported schema (spec/schema/a2a.v1.schema.json), so both accept exactly the
same values.

Mirrors typescript/packages/a2a/test/task.test.ts."""

import pytest
from pydantic import TypeAdapter, ValidationError

from threads._generated.a2a_v1 import Artifact, DataPart, Message, Part, Role, TaskStatus
from threads.a2a.protocol import text_of

_PART: TypeAdapter[Part] = TypeAdapter(Part)
_ROLE: TypeAdapter[Role] = TypeAdapter(Role)

TEXT = {"text": "hi"}


def _accepts[T](adapter: TypeAdapter[T], value: object) -> bool:
    try:
        adapter.validate_python(value)
    except (ValidationError, ValueError):
        return False
    return True


class TestAPartIsTheProtosOneof:
    @pytest.mark.parametrize(
        "part",
        [
            {"text": "hi"},
            {"raw": "AAA"},
            {"url": "https://partner.example/f.pdf"},
            {"data": {"any": "json"}},
        ],
    )
    def test_one_member_parses_whichever_it_is(self, part: dict[str, object]) -> None:
        assert _accepts(_PART, part)

    def test_the_shared_fields_ride_along_with_any_member(self) -> None:
        assert _accepts(
            _PART,
            {"text": "hi", "filename": "a.txt", "mediaType": "text/plain", "metadata": {"k": "v"}},
        )

    def test_no_member_at_all_is_refused(self) -> None:
        # It used to parse, and then read back as empty text through text_of.
        assert not _accepts(_PART, {})

    @pytest.mark.parametrize(
        "part", [{"text": "hi", "raw": "AAA"}, {"url": "https://partner.example/f", "data": 1}]
    )
    def test_two_members_at_once_are_refused(self, part: dict[str, object]) -> None:
        assert not _accepts(_PART, part)

    @pytest.mark.parametrize("raw", ["", "AAA", "AA==", "AAAA", "a-_9"])
    def test_bytes_that_are_base64_parse(self, raw: str) -> None:
        assert _accepts(_PART, {"raw": raw})

    @pytest.mark.parametrize("raw", ["!!!not base64!!!", "A", "AA=A", "AAAAA"])
    def test_bytes_that_are_not_base64_are_refused(self, raw: str) -> None:
        assert not _accepts(_PART, {"raw": raw})

    def test_a_part_with_no_text_reads_as_no_text_never_as_empty_text(self) -> None:
        assert text_of([DataPart(data={"k": 1})]) == ""


class TestPartsIsRequiredWhichMeansAtLeastOne:
    def test_a_message_with_no_parts_is_refused(self) -> None:
        assert not _accepts(
            TypeAdapter(Message), {"messageId": "m1", "role": "ROLE_USER", "parts": []}
        )

    def test_a_message_with_a_part_is_accepted(self) -> None:
        assert _accepts(
            TypeAdapter(Message), {"messageId": "m1", "role": "ROLE_USER", "parts": [TEXT]}
        )

    def test_an_artifact_with_no_parts_is_refused(self) -> None:
        # The proto's own comment on Artifact.parts says it must contain at least one part.
        assert not _accepts(TypeAdapter(Artifact), {"artifactId": "a1", "parts": []})
        assert _accepts(TypeAdapter(Artifact), {"artifactId": "a1", "parts": [TEXT]})


class TestATimestampIsRfc3339:
    @pytest.mark.parametrize(
        "stamp",
        [
            "2023-10-27T10:00:00Z",
            "2023-10-27T10:00:00.021Z",
            "2023-10-27T10:00:00+01:00",
            "2023-10-27t10:00:00z",
        ],
    )
    def test_the_forms_protobuf_timestamp_writes_are_accepted(self, stamp: str) -> None:
        assert _accepts(
            TypeAdapter(TaskStatus), {"state": "TASK_STATE_WORKING", "timestamp": stamp}
        )

    @pytest.mark.parametrize(
        "stamp", ["yesterday", "2023-10-27", "10:00:00Z", "2023-10-27T10:00:00", ""]
    )
    def test_anything_that_is_not_a_timestamp_is_refused(self, stamp: str) -> None:
        assert not _accepts(
            TypeAdapter(TaskStatus), {"state": "TASK_STATE_WORKING", "timestamp": stamp}
        )

    def test_an_absent_timestamp_is_still_fine(self) -> None:
        assert _accepts(TypeAdapter(TaskStatus), {"state": "TASK_STATE_WORKING"})


class TestAnEnumsZeroValueIsNeverAnAnswer:
    @pytest.mark.parametrize("role", ["ROLE_USER", "ROLE_AGENT"])
    def test_the_two_real_roles_are_accepted(self, role: str) -> None:
        assert _accepts(_ROLE, role)

    def test_role_unspecified_is_refused(self) -> None:
        # For the same reason TASK_STATE_UNSPECIFIED is: it says nothing we can act on.
        assert not _accepts(_ROLE, "ROLE_UNSPECIFIED")
