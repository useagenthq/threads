"""Provider responses parsed at the boundary. Providers add fields over time: unknown ones are
ignored, known ones are strict."""

from typing import ClassVar

from pydantic import BaseModel, ConfigDict


class Wire(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(extra="ignore", frozen=True, strict=True)
