# spec/

The single source of truth that **both** the TypeScript and the Python implementations obey.

- `schema/`: the event log and wire formats (JSON Schema). Field names are snake_case on the wire in both languages.
- `conformance/<case>/`: recorded runs plus expected results. Both implementations must pass every case, and a log written by one must replay identically in the other.
- `api.json` (planned): the public-name map between languages, e.g. `forkPoints` ↔ `fork_points`.

A feature starts here, before any code is written in either language.
