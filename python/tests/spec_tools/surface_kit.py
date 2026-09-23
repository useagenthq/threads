"""A small fixture contract and a fake Python package that satisfies it, for the surface gate."""

import copy
import sys
import types
from collections.abc import Generator
from contextlib import contextmanager
from typing import Protocol, Required, TypedDict, Unpack, runtime_checkable

from check_api import Json

ROOT = "fakepkg"


def _fn(ts: str, py: str, params: list[Json], **extra: Json) -> dict[str, Json]:
    out: dict[str, Json] = {
        "ts": ts,
        "py": py,
        "async": False,
        "params": params,
        "returns": {"prim": "void"},
    }
    return out | extra


def _opt(name: str, *, required: bool, **extra: Json) -> dict[str, Json]:
    out: dict[str, Json] = {
        "name": name,
        "kind": "option",
        "type": {"prim": "string"},
        "required": required,
    }
    return out | extra


def api() -> dict[str, Json]:
    """agent({model, name?}), run_sync (Python only), Model {info; send; lookup?} and Channel in
    host. A fresh copy each call, so a test can edit it."""
    return copy.deepcopy(_API)


_API: dict[str, Json] = {
    "packages": {
        "core": {"ts": "@fake/core", "py": ROOT},
        "host": {"ts": "@fake/host", "py": f"{ROOT}.host"},
    },
    "functions": {
        "agent": _fn(
            "agent", "agent", [_opt("model", required=True), _opt("name", required=False)]
        ),
        "runSync": _fn("runSync", "run_sync", [], lang="py"),
    },
    "types": {
        "Model": {
            "kind": "interface",
            "role": "protocol",
            "properties": {"info": {"type": {"prim": "string"}, "required": True}},
            "methods": {
                "send": _fn("send", "send", []),
                "lookup": _fn("lookup", "lookup", [], optional=True, capability="LooksUp"),
            },
        },
        "Channel": {"kind": "interface", "role": "protocol", "package": "host"},
    },
}


# JUnit reports and the coverage registry that matches them.
TS_JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="packages/a.test.ts">
  <testsuite name="agent"><testcase name="runs"/><testcase name="skips"><skipped/></testcase>
  </testsuite><testcase name="sends"/><testcase name="breaks"><failure/></testcase>
</testsuite></testsuites>"""
PY_JUNIT = """<?xml version="1.0"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_a" name="test_runs"/>
  <testcase classname="tests.test_a.TestModel" name="test_sends"/>
</testsuite></testsuites>"""
RUNS: dict[str, Json] = {
    "ts": ["packages/a.test.ts > agent > runs"],
    "py": ["tests/test_a.py::test_runs"],
}
SENDS: dict[str, Json] = {
    "ts": ["packages/a.test.ts > sends"],
    "py": ["tests/test_a.py::TestModel::test_sends"],
}


def coverage() -> dict[str, Json]:
    out: dict[str, Json] = {
        "agent": RUNS,
        "agent.model": RUNS,
        "Model.send": SENDS,
        "runSync": {"py": RUNS["py"]},
    }
    return out


class Model(Protocol):
    info: str

    def send(self) -> None: ...


@runtime_checkable
class LooksUp(Protocol):
    def lookup(self) -> None: ...


class Channel(Protocol):
    def verify(self) -> None: ...


class AgentOptions(TypedDict, total=False):
    model: Required[str]
    name: str


def agent(**options: Unpack[AgentOptions]) -> None:
    del options


def run_sync() -> None: ...


def _module(name: str, members: dict[str, object]) -> types.ModuleType:
    module = types.ModuleType(name)
    for key, value in members.items():
        setattr(module, key, value)
    module.__dict__["__all__"] = [k for k in members if not k.startswith("_")]
    return module


def core_members() -> dict[str, object]:
    return {"Model": Model, "LooksUp": LooksUp, "agent": agent, "run_sync": run_sync}


def host_members() -> dict[str, object]:
    return {"Channel": Channel}


@contextmanager
def installed(
    core: dict[str, object] | None = None, host: dict[str, object] | None = None
) -> Generator[None]:
    """The fake package in sys.modules for the duration: `fakepkg` and `fakepkg.host`."""
    modules = {
        ROOT: _module(ROOT, core_members() if core is None else core),
        f"{ROOT}.host": _module(f"{ROOT}.host", host_members() if host is None else host),
    }
    saved = {k: sys.modules.get(k) for k in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for key, old in saved.items():
            if old is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = old
