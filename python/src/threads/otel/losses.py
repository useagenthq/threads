"""The spans of deletion losses (spec/otel/README.md, "The cursor, sync and losses")."""

from collections.abc import Sequence

from threads.otel.ids import loss_ids
from threads.otel.span import INTERNAL, Span
from threads.store.losses import LossRow


def loss_spans(observer: str, rows: Sequence[LossRow]) -> list[Span]:
    """One zero-duration threads.export.possibly_lost span per loss row, at its deleted_at."""
    out: list[Span] = []
    for row in rows:
        trace, sid = loss_ids(observer, row.thread_id, row.deleted_at)
        attrs: dict[str, str | int | bool | tuple[str, ...] | None] = {
            "threads.tenant": row.tenant_id,
            "threads.thread_id": row.thread_id,
            "threads.unchecked_events": row.unchecked_events,
            "threads.deleted_at": row.deleted_at,
        }
        out.append(
            Span(
                trace,
                sid,
                "",
                0,
                None,
                "threads.export.possibly_lost",
                INTERNAL,
                row.deleted_at,
                row.deleted_at,
                attrs,
                (),
                (),
                None,
            )
        )
    return out
