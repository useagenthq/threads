"""Fixture contract, decisions file and adapter sources for the factory checker tests."""

import pathlib

from api_factories import Json

type Obj = dict[str, Json]

TS_SOURCE = "export const f = (k: Secret) => { k.reveal(); };\n"
PY_SOURCE = "def f(k: Secret) -> str:\n    return resolve(k)\n"


def api(*, config_errors: Json = None, params: Json = None) -> Obj:
    """A core package plus an adapter package `web` with one factory, `fetcher`."""
    return {
        "packages": {
            "core": {"ts": "@threads/core", "py": "threads", "kind": "core", "doc": "Core."},
            "web": {"ts": "@threads/core", "py": "threads", "kind": "adapter", "doc": "In core."},
        },
        "functions": {
            "agent": {"ts": "agent", "py": "agent"},
            "fetcher": {
                "ts": "fetcher",
                "py": "fetcher",
                "package": "web",
                "params": params or [],
                "config_errors": config_errors or [{"code": "missing_secret"}],
            },
        },
    }


def decisions(web: Obj | None = None, **factories: Json) -> Obj:
    return {
        "packages": {"web": web or {"ts": ["web.ts"], "py": ["web.py"]}},
        "factories": {"fetcher": {"owner": "15A"}, **factories},
        "decisions": [
            {"factory": "fetcher", "option": "x", "lang": "ts", "decision": "Seam.", "owner": "15A"}
        ],
    }


def sources(tmp_path: pathlib.Path, ts: str = TS_SOURCE, py: str = PY_SOURCE) -> pathlib.Path:
    (tmp_path / "web.ts").write_text(ts)
    (tmp_path / "web.py").write_text(py)
    return tmp_path
