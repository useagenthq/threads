"""The generated Python factory checks compile under pyright strict for a matching package, with
required keyword options and a module-qualified platform type, and fail for a drifted one."""

import json
import pathlib
import subprocess
import sys

import pytest
from gen_api_surface_factories import render_py
from typeexpr_render import Obj

PYTHON = pathlib.Path(__file__).resolve().parents[2]
API: Obj = {
    "packages": {
        "core": {"kind": "core", "py": "threads", "doc": "Core."},
        "fix": {"kind": "adapter", "py": "fixpkg", "doc": "Fixture."},
    },
    "functions": {
        "make": {
            "ts": "make",
            "py": "make",
            "package": "fix",
            "params": [
                {"name": "key", "kind": "positional", "type": {"$ref": "#/types/Secret"}},
                {"name": "region", "kind": "option", "type": {"prim": "string"}, "required": True},
                {
                    "name": "amount",
                    "kind": "option",
                    "type": {"platform": {"py": "decimal.Decimal"}},
                    "required": True,
                },
                {"name": "tier", "kind": "option", "type": {"prim": "string"}, "required": False},
            ],
            "returns": {"$ref": "#/types/SearchBackend"},
        }
    },
}
PACKAGE = """import decimal

from threads import SearchBackend, Secret
from threads.search import exa


def make(key: Secret, *, region: str, amount: decimal.Decimal, tier: str = "a") -> SearchBackend:
    return exa(key)
"""


def pyright(tmp: pathlib.Path, package: str) -> subprocess.CompletedProcess[str]:
    (tmp / "fixpkg.py").write_text(package)
    (tmp / "factories.py").write_text(render_py(API))
    config = {
        "include": ["factories.py"],
        "typeCheckingMode": "strict",
        "venvPath": str(PYTHON),
        "venv": ".venv",
        "extraPaths": [str(tmp), str(PYTHON / "src")],
    }
    (tmp / "pyrightconfig.json").write_text(json.dumps(config))
    binary = pathlib.Path(sys.executable).parent / "pyright"
    return subprocess.run(  # noqa: S603 - the venv's pyright on files this test wrote
        [str(binary), "-p", str(tmp)], capture_output=True, text=True, check=False
    )


def test_the_generated_file_names_required_options_and_platform_modules() -> None:
    source = render_py(API)
    assert "import decimal\n" in source
    assert "def returns_make(key: Secret, *, region: str, amount: decimal.Decimal)" in source
    assert "make(key, region=region, amount=amount)" in source


def test_an_optional_option_s_platform_type_is_not_imported() -> None:
    """The helper names only positional params and required options, so importing an optional
    option's module would be an unused import pyright rejects."""
    f = API["functions"]
    assert isinstance(f, dict)
    make = f["make"]
    assert isinstance(make, dict)
    params = make["params"]
    assert isinstance(params, list)
    zone: Obj = {
        "name": "zone",
        "kind": "option",
        "type": {"platform": {"py": "datetime.tzinfo"}},
        "required": False,
    }
    api: Obj = {**API, "functions": {"make": {**make, "params": [*params, zone]}}}
    assert "import datetime" not in render_py(api)


@pytest.mark.parametrize(
    ("package", "clean"),
    [
        (PACKAGE, True),
        (PACKAGE.replace("region: str,", "region: int,"), False),
        (PACKAGE.replace("-> SearchBackend:", "-> int:").replace("exa(key)", "1"), False),
    ],
    ids=["matching", "option-type", "return-type"],
)
def test_pyright_proves_the_generated_checks(
    tmp_path: pathlib.Path, package: str, *, clean: bool
) -> None:
    done = pyright(tmp_path, package)
    assert (done.returncode == 0) is clean, done.stdout
