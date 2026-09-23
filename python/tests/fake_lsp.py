"""A scripted language server over stdio for the lsp tool's tests: fixed answers, one diagnostic
published on didOpen. Run as a program by the in-sandbox driver."""

import json
import sys

type Json = dict[str, Json] | list[Json] | str | int | float | bool | None


def send(message: dict[str, Json]) -> None:
    body = json.dumps({"jsonrpc": "2.0", **message}).encode()
    sys.stdout.buffer.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    sys.stdout.buffer.flush()


def at(line: int, character: int) -> Json:
    return {"start": {"line": line, "character": character}, "end": {"line": line, "character": 9}}


def answer(method: str, params: dict[str, Json]) -> Json:
    doc = params.get("textDocument")
    uri = doc.get("uri") if isinstance(doc, dict) else None
    match method:
        case "initialize":
            return {"capabilities": {}}
        case "textDocument/definition":
            return {"uri": uri, "range": at(2, 0)}
        case "textDocument/references":
            return [{"uri": uri, "range": at(2, 0)}, {"uri": uri, "range": at(5, 4)}]
        case "textDocument/hover":
            return {"contents": {"kind": "markdown", "value": "def f() -> int"}}
        case "textDocument/documentSymbol":
            child: Json = {"name": "g", "kind": 12, "range": at(3, 4)}
            return [{"name": "f", "kind": 12, "range": at(2, 0), "children": [child]}]
        case _:
            return None


def main() -> None:
    while True:
        length = 0
        while (line := sys.stdin.buffer.readline()) not in (b"\r\n", b"\n", b""):
            name, _, value = line.decode().partition(":")
            if name.lower() == "content-length":
                length = int(value)
        if not line:
            return
        message: Json = json.loads(sys.stdin.buffer.read(length))
        if not isinstance(message, dict):
            continue
        method, params = message.get("method"), message.get("params")
        params = params if isinstance(params, dict) else {}
        if method == "exit":
            return
        if method == "textDocument/didOpen":
            doc = params.get("textDocument")
            uri = doc.get("uri") if isinstance(doc, dict) else None
            diagnostic: Json = {"range": at(0, 4), "severity": 1, "message": "boom"}
            send(
                {
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": uri, "diagnostics": [diagnostic]},
                }
            )
        if "id" in message:
            send({"id": message["id"], "result": answer(str(method), params)})


if __name__ == "__main__":
    main()
