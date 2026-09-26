"""agent(workspace=...) resolved on the host (spec/schema/README.md, Workspace inputs): what the
tree holds, what the pin says was skipped, and what is refused."""

import asyncio
import os
from pathlib import Path

import pytest
from fixture import HAS_GIT, dir_of, git, repo_of, scratch

from threads.agents.config import ConfigError
from threads.redaction.registry import register
from threads.sandbox.tree.tree import Tree, TreeFile, TreeSymlink
from threads.workspace import Forge, Resolved, Workspace, resolve_workspace

NO_FORGE = Forge(lambda repo: f"https://example.invalid/{repo}.git")
FILE = 0o644
EXEC = 0o755
needs_git = pytest.mark.skipif(not HAS_GIT, reason="the host has no git")


async def _keep(_data: bytes) -> None:
    return None


def resolve(ws: Workspace) -> Resolved:
    return asyncio.run(resolve_workspace(ws, NO_FORGE, _keep))


def paths(tree: Tree) -> list[str]:
    return [e.path for e in tree.entries]


def skipped_of(r: Resolved) -> list[str]:
    return [p for s in r.pin.sources if s.kind != "files" for p in s.skipped]


def refuses(ws: Workspace, holds: str) -> str:
    with pytest.raises(ConfigError) as caught:
        resolve(ws)
    assert caught.value.code == "invalid_config"
    assert holds in caught.value.message
    return caught.value.message


def test_files_are_taken_as_given() -> None:
    r = resolve({"files": {"NOTES.md": "# notes\n", ".env": "SECRET=1\n"}})
    assert paths(r.tree) == [".env", "NOTES.md"]
    assert all(isinstance(e, TreeFile) and e.mode == FILE for e in r.tree.entries)
    assert [s.kind for s in r.pin.sources] == ["files"]


def test_a_files_path_that_isnt_normal_is_refused() -> None:
    refuses({"files": {"../out": "x"}}, "not a normal tree path")


def test_local_dir_modes_symlinks_and_the_path_as_written() -> None:
    with scratch() as tmp:
        root = dir_of({"src/main.ts": "x", "bin/run*": "#!/bin/sh\n"}, Path(tmp))
        (root / "bin" / "link").symlink_to("../src/main.ts")
        r = resolve({"local_dir": str(root)})
        assert paths(r.tree) == ["bin", "bin/link", "bin/run", "src", "src/main.ts"]
        run_entry = next(e for e in r.tree.entries if e.path == "bin/run")
        link = next(e for e in r.tree.entries if e.path == "bin/link")
        assert isinstance(run_entry, TreeFile)
        assert run_entry.mode == EXEC
        assert isinstance(link, TreeSymlink)
        assert link.target == "../src/main.ts"
        source = r.pin.sources[0]
        assert source.kind == "local_dir"
        assert source.path == str(root)


def test_a_symlink_out_of_the_directory_is_refused() -> None:
    with scratch() as tmp:
        root = dir_of({"a.ts": "x"}, Path(tmp))
        (root / "escape").symlink_to("../../etc/passwd")
        refuses({"local_dir": str(root)}, "points outside it")


def test_a_directory_that_isnt_readable_is_refused() -> None:
    refuses({"local_dir": "/no/such/workspace/dir"}, "is not a readable directory")


def test_the_deny_list_keeps_id_utils_and_id_token() -> None:
    with scratch() as tmp:
        root = dir_of(
            {
                "id_utils/index.ts": "x",
                "id_token.ts": "x",
                "id_ed25519": "KEY",
                ".env": "A=1",
                ".npmrc": "//registry/:_authToken=t",
                "certs/server.pem": "---",
                "src/app.ts": "x",
            },
            Path(tmp),
        )
        r = resolve({"local_dir": str(root)})
        # certs/ stays: only the .pem inside it is deny-listed.
        assert paths(r.tree) == [
            "certs",
            "id_token.ts",
            "id_utils",
            "id_utils/index.ts",
            "src",
            "src/app.ts",
        ]
        assert skipped_of(r) == [".env", ".npmrc", "certs/server.pem", "id_ed25519"]


def test_more_files_than_the_limit_is_refused() -> None:
    with scratch() as tmp:
        many = Path(tmp) / "many"
        many.mkdir(parents=True)
        for i in range(10_001):
            (many / f"f{i}.txt").write_text("")
        refuses({"local_dir": tmp}, "more than 10000 files after exclusions")


def test_a_file_holding_a_registered_secret_is_refused_without_the_value() -> None:
    register("hunter2-hunter2-hunter2", "test")
    with scratch() as tmp:
        dir_of({"config.ts": "const t = 'hunter2-hunter2-hunter2';"}, Path(tmp))
        message = refuses({"local_dir": tmp}, "contains a secret value")
        assert "hunter2" not in message


def test_two_inputs_naming_one_path_is_refused() -> None:
    with scratch() as tmp:
        dir_of({"NOTES.md": "from the directory"}, Path(tmp))
        refuses(
            {"local_dir": tmp, "files": {"NOTES.md": "from files"}},
            "two inputs give NOTES.md",
        )


@needs_git
def test_git_and_gitignored_paths_never_reach_the_tree() -> None:
    with scratch() as tmp:
        root = repo_of(
            {
                ".gitignore": "node_modules/\n.env.test\n",
                "node_modules/left-pad/index.js": "module.exports = 1",
                "node_modules/.env": "IN=modules",
                ".env": "A=1",
                ".env.test": "B=2",
                "id_rsa": "KEY",
                ".npmrc": "//registry/:_authToken=t",
                "src/app.ts": "x",
            },
            Path(tmp),
        )
        with (root / ".git" / "config").open("a") as out:
            out.write('[http]\n  extraheader = "Authorization: Basic c2VjcmV0"\n')
        r = resolve({"local_dir": str(root)})
        assert paths(r.tree) == [".gitignore", "src", "src/app.ts"]
        assert skipped_of(r) == [".env", ".env.test", ".git", ".npmrc", "id_rsa", "node_modules"]
        assert "extraheader" not in r.pin.model_dump_json()


@needs_git
def test_include_re_admits_one_gitignored_file() -> None:
    with scratch() as tmp:
        root = repo_of(
            {
                ".gitignore": ".env.test\nnode_modules/\n",
                ".env.test": "B=2",
                "node_modules/a.js": "1",
                "src/app.ts": "x",
            },
            Path(tmp),
        )
        r = resolve({"local_dir": str(root), "include": [".env.test"]})
        assert paths(r.tree) == [".env.test", ".gitignore", "src", "src/app.ts"]


@needs_git
def test_include_on_a_directory_re_admits_its_subtree() -> None:
    with scratch() as tmp:
        root = repo_of(
            {
                ".gitignore": "node_modules/\n",
                "node_modules/left-pad/index.js": "1",
                "node_modules/left-pad/.env": "A=1",
                "node_modules/.bin/tsc*": "#!/bin/sh\n",
                "node_modules/vendored/.git/config": "[core]\n",
                "src/app.ts": "x",
            },
            Path(tmp),
        )
        r = resolve({"local_dir": str(root), "include": ["node_modules"]})
        got = paths(r.tree)
        assert "node_modules/left-pad/index.js" in got
        assert "node_modules/.bin/tsc" in got
        assert "node_modules/left-pad/.env" not in got
        assert not any(".git/" in p for p in got)


@needs_git
def test_an_include_that_re_admits_nothing_is_refused() -> None:
    with scratch() as tmp:
        root = repo_of({"src/app.ts": "x"}, Path(tmp))
        refuses({"local_dir": str(root), "include": ["nothing.txt"]}, "re-admits nothing")


@needs_git
def test_include_cant_name_dot_git() -> None:
    with scratch() as tmp:
        root = repo_of({"src/app.ts": "x"}, Path(tmp))
        refuses(
            {"local_dir": str(root), "include": [".git/config"]},
            "a normal tree path outside .git",
        )


@needs_git
def test_a_submodule_is_refused_naming_it() -> None:
    with scratch() as outer, scratch() as inner_dir:
        root = repo_of({"src/app.ts": "x"}, Path(outer))
        inner = repo_of({"lib.ts": "x"}, Path(inner_dir))
        git(inner, "add", "-A")
        git(inner, "commit", "--quiet", "-m", "one")
        git(
            root,
            "-c",
            "protocol.file.allow=always",
            "submodule",
            "--quiet",
            "add",
            str(inner),
            "vendor",
        )
        refuses({"local_dir": str(root)}, "submodules aren't copied")


@needs_git
def test_a_subdirectory_of_a_repository_honors_its_gitignore() -> None:
    with scratch() as tmp:
        root = repo_of(
            {".gitignore": "build/\n", "pkg/build/out.js": "1", "pkg/src/app.ts": "x"},
            Path(tmp),
        )
        r = resolve({"local_dir": str(root / "pkg")})
        assert paths(r.tree) == ["src", "src/app.ts"]
        assert skipped_of(r) == ["build"]


@needs_git
def test_a_work_tree_with_no_git_on_path_is_refused() -> None:
    with scratch() as tmp:
        root = repo_of({"src/app.ts": "x"}, Path(tmp))
        path = os.environ["PATH"]
        os.environ["PATH"] = "/nonexistent"
        try:
            refuses({"local_dir": str(root)}, "install git, or point localDir")
        finally:
            os.environ["PATH"] = path
