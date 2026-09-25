"""The portable subset's `?` placeholders in psycopg's form: `%s`, with every literal `%`
doubled, outside quoted literals and identifiers (spec/conformance/vectors/sql-placeholders.json).
"""

from functools import cache


@cache
def psycopg(sql: str) -> str:
    out: list[str] = []
    quote: str | None = None
    for char in sql:
        if quote is not None:
            if char == quote:
                quote = None
            out.append("%%" if char == "%" else char)
        elif char in "'\"":
            quote = char
            out.append(char)
        elif char == "?":
            out.append("%s")
        else:
            out.append("%%" if char == "%" else char)
    return "".join(out)
