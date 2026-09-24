"""The drafts of one team append, each with its event id minted before the append: later events of
the same batch name earlier ones (a monitor's registering event, a mail's causal event), and a
runtime mail's id is its message_sent's event id."""

from collections.abc import Callable

from threads.store.lines import Draft, uuid7

type Mint = Callable[[int, int], str]
"""The event id of the event a batch appends at (seq, now). The runtime's is a UUIDv7 from the
append's clock; a test injects a deterministic one to compare ids with the op vectors."""


def mint_uuid7(_seq: int, now: int) -> str:
    return uuid7(now)


_TAKES = frozenset({"message_received", "mail_refused", "user_input"})


class Batch:
    def __init__(self, head: int, now: int, mint: Mint | None = None) -> None:
        """A batch appended after the committed event at `head`."""
        self.drafts: list[Draft] = []
        self._head = head
        self._now = now
        self._mint = mint or mint_uuid7
        self._next: str | None = None

    def next_id(self) -> str:
        """The event id the next added draft gets."""
        if self._next is None:
            self._next = self._mint(self._head + len(self.drafts) + 1, self._now)
        return self._next

    def taken(self) -> frozenset[str]:
        """The mail this batch already takes (a receipt, a task's input, a refusal): the index
        moves those rows only when the batch commits, so a later read in the batch skips them."""
        return frozenset(
            mail
            for d in self.drafts
            if d.type in _TAKES and isinstance(mail := d.data.get("mail_id"), str)
        )

    def add(self, draft: Draft) -> str:
        """Adds a draft and returns its event id: its own, or one minted now."""
        event_id = draft.event_id or self.next_id()
        self._next = None
        self.drafts.append(Draft(draft.type, draft.data, draft.actor, draft.critical, event_id))
        return event_id
