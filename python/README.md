# threads (Python)

The Python implementation of threads. It must pass everything in `../spec/conformance/`.
Rules: `../AGENTS.md` (Python section, to be written: Pydantic v2, pyright strict, ruff, pytest).

## Generated models

`src/threads/_generated/events_v1.py` is generated from `../spec/schema/events.v1.schema.json`. Never edit it by hand.

```sh
uv run python tools/regen_models.py          # regenerate
uv run python tools/regen_models.py --check  # CI: fail if stale
```
