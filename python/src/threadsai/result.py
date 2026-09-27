"""Expected failures as values: a function returns `Ok[T] | Err[E]` instead of raising."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Ok[T]:
    value: T


@dataclass(frozen=True, slots=True)
class Err[E]:
    error: E
