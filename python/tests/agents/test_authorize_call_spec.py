"""Plan mode; a read_file call is recorded, then a tools_changed removes read_file. The policy
decides the call by the spec it was made under (read_only: allowed in plan mode), the same
decision TypeScript's authorize makes (test/agent/authorize-call-spec.test.ts)."""

from corpus import CASES, own

from threads.agents.bindings import authorize
from threads.log import CallId
from threads.reduce.fold import call_spec
from threads.result import Ok
from threads.store import verify_export

CASE = CASES / "recover-removed-tool-call-not-executed"
NOW = 1_790_000_060_000


def test_authorize_decides_a_call_by_its_call_time_spec() -> None:
    verified = verify_export(own(CASE, "log.jsonl").read_bytes(), NOW)
    assert isinstance(verified, Ok)
    fold = verified.value.fold
    call_id = CallId("call_1")
    spec = call_spec(fold, call_id)
    assert spec is not None
    assert "read_file" not in fold.tools
    assert authorize(fold, fold.calls[call_id].data, spec).decision == "allow"
