"""spec/tools/check_model_catalog.py: released catalog entries are append-only, evidence can be
refreshed forward only, and withdrawals are locked too."""

from check_model_catalog import MODELS, Obj, behavior, check, main

ENTRY: Obj = {
    "id": "m-1",
    "max_input_tokens": 1000,
    "max_output_tokens": 100,
    "source": "https://example.com/m-1",
    "verified": "2026-09-24",
}
WITHDRAWAL: Obj = {"id": "m-1", "reason": "wrong", "use": {"max_input_tokens": 900}}


def catalog(*entries: Obj, withdrawn: tuple[Obj, ...] = ()) -> Obj:
    return {"version": 1, "provider": "p", "entries": list(entries), "withdrawn": list(withdrawn)}


LOCK: Obj = check(catalog(ENTRY), {}, add=True).lock


def lines(lock: Obj) -> Obj:
    found = lock["entries"]
    assert isinstance(found, dict)
    return found


def test_the_committed_catalogs_match_their_locks() -> None:
    assert sorted(p.name for p in MODELS.glob("*.v1.json")) == [
        "anthropic.v1.json",
        "openai.v1.json",
    ]
    assert main([]) == 0


def test_add_appends_only_new_ids() -> None:
    other = {**ENTRY, "id": "m-2"}
    out = check(catalog(ENTRY, other), LOCK, add=True)
    assert out.problems == ()
    assert set(lines(out.lock)) == {"m-1", "m-2"}
    assert lines(out.lock)["m-1"] == lines(LOCK)["m-1"]


def test_an_unlocked_entry_fails() -> None:
    assert check(catalog(ENTRY), {}).problems == (
        "m-1: not locked yet; run check_model_catalog.py --add",
    )


def test_a_changed_behavioral_field_fails_even_with_the_flags() -> None:
    changed = {**ENTRY, "max_input_tokens": 999}
    for flags in ({}, {"add": True, "refresh": True}):
        assert check(catalog(changed), LOCK, **flags).problems == (
            "m-1: a released entry's id, limits and alias_of never change",
        )
    aliased = {**ENTRY, "alias_of": "m-0"}
    assert check(catalog(aliased), LOCK).problems != ()


def test_a_refreshed_source_passes() -> None:
    assert check(catalog({**ENTRY, "source": "https://example.com/new"}), LOCK).problems == ()


def test_a_later_date_needs_refresh_which_moves_only_the_date() -> None:
    later = {**ENTRY, "verified": "2026-10-01"}
    assert check(catalog(later), LOCK).problems == (
        "m-1: verified moved to 2026-10-01; run check_model_catalog.py --refresh",
    )
    out = check(catalog(later), LOCK, refresh=True)
    assert out.problems == ()
    assert lines(out.lock)["m-1"] == {
        "behavior_sha256": behavior(ENTRY),
        "verified": "2026-10-01",
    }
    assert check(catalog(later), out.lock).problems == ()


def test_an_earlier_date_fails_even_with_refresh() -> None:
    earlier = {**ENTRY, "verified": "2026-01-01"}
    for refresh in (False, True):
        assert check(catalog(earlier), LOCK, refresh=refresh).problems == (
            "m-1: verified 2026-01-01 is earlier than the locked 2026-09-24",
        )


def test_a_removed_entry_fails() -> None:
    assert check(catalog(), LOCK).problems == ("m-1: a locked entry was removed",)


def test_withdrawals_are_locked_append_only() -> None:
    withdrawn = catalog(ENTRY, withdrawn=(WITHDRAWAL,))
    assert check(withdrawn, LOCK).problems == (
        "m-1: withdrawal not locked yet; run check_model_catalog.py --add",
    )
    sealed = check(withdrawn, LOCK, add=True).lock
    assert check(withdrawn, sealed).problems == ()
    assert check(catalog(ENTRY), sealed).problems == ("m-1: a locked withdrawal was removed",)
    changed: Obj = {**WITHDRAWAL, "use": {"max_input_tokens": 800}}
    assert check(catalog(ENTRY, withdrawn=(changed,)), sealed).problems == (
        "m-1: a locked withdrawal's reason and use never change",
    )
