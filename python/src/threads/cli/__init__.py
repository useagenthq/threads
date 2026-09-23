"""The `threads` CLI (spec/api.json cli, ): a thin wrapper over the typed
API. Serving needs the `host` extra.

    threads dev [module] [--port 8787]      serve on localhost, print each channel's webhook URL
    threads start [module] [--port 8000]    the same for production, on all interfaces
    threads timeline <thread_id> [--branch <branch_id>]
    threads export <branch_id>              the branch's JSONL export on stdout
    threads import <file>                   verify an export and store its bytes
    threads repair <branch_id>              make a torn import runnable (log_repaired)
    threads delete <thread_id> | --tenant <tenant_id>   delete a thread, or a tenant's threads
    threads gc [--grace-days <n>] [module]  release ledger rows, sweep unreferenced artifacts

`module` is `module`, `module:attribute` or `file.py[:attribute]`, default `app`. The store is
`--store` (default `.threads`) scoped to `--tenant` (default `local`).
"""

import argparse
import asyncio
import os
import sys
from collections.abc import Callable, Coroutine, Sequence

from threads.cli import serve, store
from threads.store import LOCAL_TENANT


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="threads", description="The threads host and store CLI.")
    parser.add_argument("--store", default=".threads", help="store directory (default .threads)")
    parser.add_argument("--tenant", default=LOCAL_TENANT, help="tenant (default local)")
    commands = parser.add_subparsers(dest="command", required=True)
    for name, port in (("dev", 8787), ("start", int(os.environ.get("PORT", "8000")))):
        server = commands.add_parser(name, help=f"serve the host ({name})")
        server.add_argument("module", nargs="?", default="app")
        server.add_argument("--port", type=int, default=port)
    shown = commands.add_parser("timeline", help="print a thread's steps")
    shown.add_argument("thread_id")
    shown.add_argument("--branch")
    commands.add_parser("export", help="export a branch").add_argument("branch_id")
    commands.add_parser("import", help="import an export").add_argument("file")
    commands.add_parser("repair", help="repair a torn import").add_argument("branch_id")
    removal = commands.add_parser("delete", help="delete a thread or a tenant's threads")
    removal.add_argument("thread_id", nargs="?")
    removal.add_argument("--tenant", dest="all_of", help="delete every thread of this tenant")
    collect = commands.add_parser("gc", help="release resources, sweep artifacts")
    collect.add_argument("module", nargs="?", help="the host module whose sandboxes release")
    collect.add_argument("--grace-days", type=float, default=7.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command = str(args.command)
    if command in ("dev", "start"):
        return _serve(command, str(args.module), int(args.port))
    if command == "delete" and (args.thread_id is None) == (args.all_of is None):
        print("delete needs a thread_id or --tenant <tenant_id>", file=sys.stderr)
        return 2
    return asyncio.run(_command(command, args))


def _command(command: str, args: argparse.Namespace) -> Coroutine[object, object, int]:
    path, tenant = str(args.store), str(args.tenant)

    def optional(name: str) -> str | None:
        value = getattr(args, name, None)
        return None if value is None else str(value)

    commands: dict[str, Callable[[], Coroutine[object, object, int]]] = {
        "timeline": lambda: store.timeline(path, tenant, str(args.thread_id), optional("branch")),
        "export": lambda: store.export(path, tenant, str(args.branch_id)),
        "import": lambda: store.import_(path, tenant, str(args.file)),
        "repair": lambda: store.repair(path, tenant, str(args.branch_id)),
        "delete": lambda: store.delete(path, optional("all_of") or tenant, optional("thread_id")),
        "gc": lambda: store.gc(path, optional("module"), float(args.grace_days)),
    }
    return commands[command]()


def _serve(command: str, module: str, port: int) -> int:
    served = serve.load(module)
    bind = "127.0.0.1" if command == "dev" else "0.0.0.0"  # noqa: S104 - start serves the network
    base = f"http://localhost:{port}"
    for url in serve.webhook_urls(served, base):
        print(f"webhook: {url}")
    serve.serve(served, bind, port)
    return 0
