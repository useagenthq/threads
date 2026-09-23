"""An ask_user answer's recorded preview (spec/schema/README.md, Multi-choice)."""

from threads.thread.control import answer_text


def test_a_multi_choice_answer_joins_with_newlines_so_commas_stay_inside_a_choice() -> None:
    assert answer_text(["staging, eu", "production"]) == "staging, eu\nproduction"
    assert answer_text("Staging.") == "Staging."
