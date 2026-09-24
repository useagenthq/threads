"""Each provider's model catalog (spec/models/<provider>.v1.json): the limits of exact model ids,
parsed from the bytes generate.sh embeds, so the runtime reads no file under spec/."""

from collections.abc import Mapping, Sequence
from typing import Final

from pydantic import ValidationError

from threads._generated.model_catalog_v1 import ModelCatalog
from threads._generated.model_catalogs import MODEL_CATALOGS
from threads.result import Err, Ok

__all__ = ["CATALOGS", "ModelCatalog", "parse_catalog"]


def parse_catalog(text: str) -> Ok[ModelCatalog] | Err[str]:
    """One catalog file: the schema, then the rule JSON Schema can't state (unique ids)."""
    try:
        catalog = ModelCatalog.model_validate_json(text)
    except ValidationError as error:
        return Err(str(error))
    repeated = _duplicate([e.id for e in catalog.entries]) or _duplicate(
        [w.id for w in catalog.withdrawn]
    )
    return Ok(catalog) if repeated is None else Err(f'model id "{repeated}" is listed twice')


def _duplicate(ids: Sequence[str]) -> str | None:
    return next((i for n, i in enumerate(ids) if i in ids[:n]), None)


def _load(text: str) -> ModelCatalog:
    parsed = parse_catalog(text)
    if isinstance(parsed, Err):
        # The bytes were checked in CI: a failure here is a broken build, not a user error.
        raise RuntimeError(f"embedded model catalog: {parsed.error}")
    return parsed.value


CATALOGS: Final[Mapping[str, ModelCatalog]] = {
    c.provider: c for c in (_load(text) for text in MODEL_CATALOGS)
}
