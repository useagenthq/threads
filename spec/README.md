# spec/

The single source of truth that **both** the TypeScript and the Python implementations obey.

- `schema/`: the log line and wire formats (JSON Schema, exported from the Zod source; Pydantic generated from it). snake_case, RFC 8785 lines. SQLite stores these exact line bytes and JSONL exports carry the same bytes, so storage, export and fixtures are one contract. See `schema/README.md`.
- `conformance/cases/<name>/`: input logs, scripts and expected results. Both implementations must pass every case, and a log written by one must replay identically in the other. See `conformance/README.md`.
- `tools/`: `gen_fixtures.py` (thin entry point; `--check` fails CI on any drift) runs the `fixtures/` package: `jcs.py` (restricted canonical JSON with UTF-16 key order), one module per case family, and `__main__.py`. Stdlib Python; passes the `python/` project pyright and ruff config.
- `api.json` (**not written yet; a pre-implementation milestone**): the public API contract between languages: names (`forkPoints` ↔ `fork_points`), parameters, optionality and defaults, async result types and typed failures. It must exist, with `schema/host-api/`, before either language writes public API code (`agent()`, `run()`, `stream()`, the thread handle, `host()`). Work on the wire schema, parsing, canonical JSON, the store and the conformance runners doesn't need it. Until it exists, the handoff is not complete.
- `conformance/coverage.json`: every brief scenario mapped to a case, test, job or live gate, with `planned` or `implemented` status. See `conformance/README.md`.

A feature starts here, before any code is written in either language.
