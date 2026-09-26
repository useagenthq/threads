"""`PosInt` (spec/api.json, `urn:threads:schema:events:v1#/$defs/PosInt`): a whole number above
zero. The one check a public entry point makes on such an option, before anything is written."""

from threads.log.jcs import MAX_SAFE_INTEGER


def is_pos_int(value: object) -> bool:
    """Whether `value` is a positive integer.

    A public argument is untyped until this says otherwise: the declared `int` is only what the
    checker sees. `bool` is an `int` subclass, so `True` would otherwise pass as 1; a float, a
    NaN and a numeric string are values the writer would reject far later, as a raise. Zero is
    not positive, so a zero timeout is a refusal, never "use the default".
    """
    return isinstance(value, int) and not isinstance(value, bool) and 0 < value <= MAX_SAFE_INTEGER
