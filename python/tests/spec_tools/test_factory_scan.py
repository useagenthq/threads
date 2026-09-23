"""The refusal source scan over adapter packages (spec/tools/factory_scan.py)."""

import pathlib

import pytest
from api_factories import Json
from factory_decisions import check_decisions
from factory_kit import PY_SOURCE, TS_SOURCE, Obj, api, decisions, sources
from factory_scan import raised_codes

SITES: Obj = {"web.ts": 1, "web.py": 1}


def gapped(sites: Obj = SITES) -> Obj:
    gap: Obj = {"owner": "09", "why": "Setup.", "sites": sites}
    return {"ts": ["web.ts"], "py": ["web.py"], "gaps": {"missing_secret": gap}}


INVALID: Json = [{"code": "invalid_config"}]


def test_a_raised_code_missing_from_config_errors_is_red(tmp_path: pathlib.Path) -> None:
    ts = TS_SOURCE + 'throw new ConfigError("invalid_config", "bad");\n'
    assert check_decisions(api(), decisions(), sources(tmp_path, ts=ts)) == [
        "api.json fetcher: web sources can raise invalid_config (ts, web.ts); "
        "declare it in config_errors"
    ]


def test_a_code_declared_for_the_other_language_only_is_red(tmp_path: pathlib.Path) -> None:
    errors: Json = [{"code": "missing_secret", "lang": "ts"}]
    assert check_decisions(api(config_errors=errors), decisions(), sources(tmp_path)) == [
        "api.json fetcher: web sources can raise missing_secret (py, web.py); "
        "declare it in config_errors"
    ]


def test_a_scalar_source_list_is_refused_not_skipped(tmp_path: pathlib.Path) -> None:
    doc = decisions({"ts": ["web.ts"], "py": "web.py"})
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions packages.web.py: expected a non-empty list of paths"
    ]
    doc = decisions({"ts": [], "py": ["web.py"]})
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions packages.web.ts: expected a non-empty list of paths"
    ]


def test_a_directory_is_scanned_whole(tmp_path: pathlib.Path) -> None:
    (tmp_path / "pkg" / "deep").mkdir(parents=True)
    (tmp_path / "pkg" / "deep" / "new.ts").write_text('new ConfigError("invalid_config", "m")')
    sources(tmp_path)
    doc = decisions({"ts": ["web.ts", "pkg"], "py": ["web.py"]})
    assert check_decisions(api(), doc, tmp_path) == [
        "api.json fetcher: web sources can raise invalid_config (ts, pkg/deep/new.ts); "
        "declare it in config_errors"
    ]


def test_a_missing_source_is_red(tmp_path: pathlib.Path) -> None:
    (tmp_path / "web.ts").write_text(TS_SOURCE)
    assert check_decisions(api(), decisions(), tmp_path) == [
        "decisions packages.web: web.py does not exist"
    ]


def test_a_pinned_gap_passes_until_declared(tmp_path: pathlib.Path) -> None:
    doc = decisions(gapped())
    assert check_decisions(api(config_errors=INVALID), doc, sources(tmp_path)) == []
    assert check_decisions(api(), doc, sources(tmp_path)) == [
        "decisions packages.web.gaps.missing_secret: fetcher declares it now; drop the gap"
    ]


def test_a_new_site_of_a_gapped_code_is_red(tmp_path: pathlib.Path) -> None:
    py = 'def f(k):\n    return resolve(k)\n\nraise ConfigError("missing_secret", "new")\n'
    doc = decisions(gapped())
    assert check_decisions(api(config_errors=INVALID), doc, sources(tmp_path, py=py)) == [
        "decisions packages.web.gaps.missing_secret: sites {'web.py': 2, 'web.ts': 1} != the "
        "gap's {'web.ts': 1, 'web.py': 1}; declare missing_secret in config_errors or update "
        "the gap"
    ]


def test_a_keyword_construction_counts_against_the_gap(tmp_path: pathlib.Path) -> None:
    py = PY_SOURCE + 'raise ConfigError(code="missing_secret", message="new")\n'
    doc = decisions(gapped())
    assert check_decisions(api(config_errors=INVALID), doc, sources(tmp_path, py=py)) == [
        "decisions packages.web.gaps.missing_secret: sites {'web.py': 2, 'web.ts': 1} != the "
        "gap's {'web.ts': 1, 'web.py': 1}; declare missing_secret in config_errors or update "
        "the gap"
    ]


def test_a_construction_the_scan_cannot_read_is_red(tmp_path: pathlib.Path) -> None:
    py = PY_SOURCE + "raise ConfigError(CODE, 'computed')\n"
    doc = decisions(gapped())
    assert check_decisions(api(config_errors=INVALID), doc, sources(tmp_path, py=py)) == [
        "decisions packages.web: web.py constructs a ConfigError the scan can't count; only "
        "construct (code as a string literal), import or catch it by name"
    ]


def test_an_adapter_package_must_list_its_own_sources(tmp_path: pathlib.Path) -> None:
    own = api()
    packages = own["packages"]
    assert isinstance(packages, dict)
    packages["web"] = {"ts": "@threads/web", "py": "threads.web", "kind": "adapter", "doc": "W."}
    assert check_decisions(own, decisions(), sources(tmp_path)) == [
        "decisions packages.web.ts: list typescript/packages/web/src, the package's own "
        "sources, so a new file there is scanned",
        "decisions packages.web.py: list python/src/threads/web, the package's own sources, "
        "so a new file there is scanned",
    ]


def test_a_gap_without_owner_or_sites_is_refused(tmp_path: pathlib.Path) -> None:
    web: Obj = {"ts": ["web.ts"], "gaps": {"missing_secret": {"owner": "09", "why": "Later."}}}
    assert check_decisions(api(), decisions(web), sources(tmp_path)) == [
        "decisions packages.web.gaps.missing_secret: expected {owner, why, sites: {path: count}}"
    ]


def test_the_scan_counts_direct_codes_and_core_helpers() -> None:
    ts = (
        'x.reveal(); y.reveal(); throw new ConfigError("invalid_config", "m"); checkHostedTools(t);'
    )
    assert raised_codes(ts, "ts") == {
        "missing_secret": 2,
        "invalid_config": 1,
        "hosted_tool_unsupported": 1,
    }
    py = 'resolve(k)\nsystem_resolve(h)\nself.resolve(x)\nraise ConfigError("unknown_preset", "m")'
    assert raised_codes(py, "py") == {"missing_secret": 1, "unknown_preset": 1}
    assert raised_codes('credential("exa", "apiKey", k, "EXA")', "ts") == {"missing_secret": 1}
    assert raised_codes('credential("exa", "api_key", k, "EXA")', "py") == {"missing_secret": 1}
    qualified = "secrets.resolve(k)\nraise ConfigError(code='invalid_config', message='m')"
    assert raised_codes(qualified, "py") == {"missing_secret": 1, "invalid_config": 1}
    assert raised_codes("raise ConfigError(code, message)", "py") == {"<unreadable>": 1}


PY_IMPORT_AS = "from threads.agents.config import ConfigError as Error\n"
TS_IMPORT_AS = 'import { ConfigError as E } from "../agent/errors";\n'
RAISE = 'raise Error("missing_secret", "m")'
THROW = 'throw new E("missing_secret", "m");'


@pytest.mark.parametrize(
    ("source", "lang"),
    [
        (PY_IMPORT_AS + RAISE, "py"),
        ("Error = ConfigError\n" + RAISE, "py"),
        ("class Refusal(ConfigError):\n    pass", "py"),
        (TS_IMPORT_AS + THROW, "ts"),
        ("const E = ConfigError;\n" + THROW, "ts"),
        ("class Refusal extends ConfigError {}", "ts"),
        ("(Alias,) = (ConfigError,)\n" + RAISE.replace("Error", "Alias", 1), "py"),
        ("import threads.agents.config as c\nE = c.ConfigError\n" + RAISE, "py"),
        ("class Refusal(ValueError, ConfigError):\n    pass", "py"),
        ("try:\n    f()\nexcept ValueError: E = ConfigError\n" + RAISE, "py"),
        ('refusals = {"e": ConfigError}\n', "py"),
        ("const [E] = [ConfigError];\n" + THROW, "ts"),
        ("const make = { E: ConfigError };\n", "ts"),
        ("const E = ok ? Other : ConfigError;\n" + THROW, "ts"),
        ('alias = {"#": ConfigError}["#"]\nraise alias("missing_secret", "m")\n', "py"),
        ('const E = { "https://x": ConfigError }["https://x"];\n' + THROW, "ts"),
        ("// a comment naming ConfigError\n", "ts"),
    ],
    ids=[
        "py-import-as",
        "py-assign",
        "py-subclass",
        "ts-import-as",
        "ts-assign",
        "ts-extends",
        "py-tuple",
        "py-qualified",
        "py-second-base",
        "py-after-except",
        "py-dict",
        "ts-array",
        "ts-object",
        "ts-ternary",
        "py-hash-in-string",
        "ts-slashes-in-string",
        "ts-comment",
    ],
)
def test_an_alias_or_subclass_of_config_error_is_unreadable(source: str, lang: str) -> None:
    assert raised_codes(source, lang)["<unreadable>"] >= 1


def test_catching_and_plain_imports_are_not_aliases() -> None:
    py = (
        "from threads.agents.config import ConfigError\n"
        "try:\n    f()\nexcept ConfigError as e:\n    pass\n"
    )
    assert raised_codes(py, "py") == {}
    ts = 'import { ConfigError } from "./errors";\nif (e instanceof ConfigError) {}\n'
    assert raised_codes(ts, "ts") == {}
    caught = "if isinstance(error, ConfigError):\n    pass  # ConfigError\n"
    assert raised_codes(caught, "py") == {}


def test_a_python_comment_is_never_a_site() -> None:
    py = '# ConfigError("missing_secret", "x") and resolve(k) in a comment\nresolve(k)\n'
    assert raised_codes(py, "py") == {"missing_secret": 1}
