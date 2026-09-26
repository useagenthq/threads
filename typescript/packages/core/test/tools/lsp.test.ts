import { afterAll, beforeAll, describe, expect, test } from "bun:test";
import {
  chmodSync,
  mkdirSync,
  mkdtempSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { agent, fakeSandbox, scriptedModel } from "../../src";
import { answered, lspText, lspWith } from "../../src/tools/lsp";
import { LSP_SEARCH_PATH } from "../../src/tools/lsp-driver";
import { bound, localSession } from "./kit";

// lsp (F1.19): the in-sandbox driver against a scripted language server; a
// server that is missing or never ready is `unavailable`, never an empty success.

// A tiny stdio language server: asks the client for configuration first (the driver must
// answer), publishes one diagnostic on open, answers hover, definition and documentSymbol.
const SERVER = String.raw`
import json, sys
inp, out = sys.stdin.buffer, sys.stdout.buffer
def send(m):
    b = json.dumps(m).encode()
    out.write(b"Content-Length: %d\r\n\r\n" % len(b) + b); out.flush()
def recv():
    n = None
    while True:
        line = inp.readline()
        if not line: sys.exit(0)
        line = line.strip()
        if not line: break
        if line.lower().startswith(b"content-length:"): n = int(line.split(b":")[1])
    return json.loads(inp.read(n))
while True:
    m = recv()
    meth = m.get("method")
    if meth == "initialize":
        send({"jsonrpc": "2.0", "id": 99, "method": "workspace/configuration", "params": {"items": [{}]}})
        r = recv()
        assert r.get("id") == 99 and r["result"] == [None], r
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"capabilities": {}}})
    elif meth == "textDocument/didOpen":
        uri = m["params"]["textDocument"]["uri"]
        send({"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics", "params": {"uri": uri, "diagnostics": [
            {"range": {"start": {"line": 2, "character": 4}, "end": {"line": 2, "character": 5}}, "severity": 1, "message": "x is undefined"}]}})
    elif meth == "textDocument/hover":
        p = m["params"]["position"]
        send({"jsonrpc": "2.0", "id": m["id"], "result": {"contents": {"kind": "plaintext", "value": "hover at %d:%d" % (p["line"], p["character"])}}})
    elif meth == "textDocument/definition":
        send({"jsonrpc": "2.0", "id": m["id"], "result": [{"uri": m["params"]["textDocument"]["uri"], "range": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}}}]})
    elif meth == "textDocument/documentSymbol":
        send({"jsonrpc": "2.0", "id": m["id"], "result": [{"name": "Box", "kind": 5, "range": {"start": {"line": 0, "character": 0}, "end": {"line": 9, "character": 0}},
              "selectionRange": {"start": {"line": 0, "character": 0}, "end": {"line": 0, "character": 3}},
              "children": [{"name": "open", "kind": 6, "range": {"start": {"line": 3, "character": 2}, "end": {"line": 4, "character": 0}}}]}]})
    elif meth == "shutdown":
        send({"jsonrpc": "2.0", "id": m["id"], "result": None})
    elif meth == "exit":
        sys.exit(0)
`;

let root = "";
beforeAll(() => {
  root = mkdtempSync(join(tmpdir(), "threads-lsp-test-"));
});
afterAll(() => rmSync(root, { recursive: true, force: true }));

function tool(command: readonly string[]) {
  const session = localSession(join(root, "sbx"));
  writeFileSync(
    join(root, "sbx/workspace/app.fake"),
    "class Box:\n  pass\n    x\n",
  );
  return bound(
    lspWith([{ command, extensions: { ".fake": "fake" } }]),
    session,
  );
}

describe("lsp through the in-sandbox driver", () => {
  test("diagnostics, hover, definition and symbols, 1-based", async () => {
    writeFileSync(join(root, "server.py"), SERVER);
    const lsp = tool(["python3", join(root, "server.py")]);
    const path = join(root, "sbx/workspace/app.fake");
    expect(
      await lsp.run({ operation: "diagnostics", path: "app.fake" }),
    ).toEqual({
      kind: "done",
      output: "/workspace/app.fake:3:5 error: x is undefined",
      isError: false,
    });
    expect(
      await lsp.run({
        operation: "hover",
        path: "app.fake",
        line: 3,
        character: 5,
      }),
    ).toMatchObject({ output: "hover at 2:4" });
    expect(
      await lsp.run({
        operation: "definition",
        path: "app.fake",
        line: 1,
        character: 1,
      }),
    ).toMatchObject({ output: `${path}:1:1` });
    expect(
      await lsp.run({ operation: "symbols", path: "app.fake" }),
    ).toMatchObject({
      output: "Box (kind 5) line 1\n  open (kind 6) line 4",
    });
  }, 30_000);

  test("a server given by absolute path runs from outside the searched dirs", async () => {
    // Like /opt/... or /workspace/node_modules/.bin/...: an absolute command is run as given.
    writeFileSync(join(root, "server.py"), SERVER);
    mkdirSync(join(root, "opt/bin"), { recursive: true });
    symlinkSync(
      Bun.which("python3") ?? "python3",
      join(root, "opt/bin/python3"),
    );
    const lsp = tool([join(root, "opt/bin/python3"), join(root, "server.py")]);
    expect(
      await lsp.run({ operation: "symbols", path: "app.fake" }),
    ).toMatchObject({
      output: "Box (kind 5) line 1\n  open (kind 6) line 4",
      isError: false,
    });
  }, 30_000);

  test("an env-shebang launcher finds its interpreter on the fixed system PATH", async () => {
    // typescript-language-server and pyright-langserver start with `#!/usr/bin/env node`: the
    // server needs a PATH, and it must be exactly the fixed system dirs, never the host's.
    writeFileSync(join(root, "server.py"), SERVER);
    const launcher = join(root, "opt/launcher");
    mkdirSync(join(root, "opt"), { recursive: true });
    writeFileSync(
      launcher,
      [
        "#!/usr/bin/env python3",
        "import os, sys",
        `if os.environ.get("PATH") != ${JSON.stringify(LSP_SEARCH_PATH)}: sys.exit("PATH is not the fixed dirs")`,
        `os.execvp("python3", ["python3", ${JSON.stringify(join(root, "server.py"))}])`,
        "",
      ].join("\n"),
    );
    chmodSync(launcher, 0o755);
    expect(
      await tool([launcher]).run({ operation: "symbols", path: "app.fake" }),
    ).toMatchObject({
      output: "Box (kind 5) line 1\n  open (kind 6) line 4",
      isError: false,
    });
  }, 60_000);

  test("a missing server, an undeclared file type, or a missing position is an error result", async () => {
    const lsp = tool(["threads-no-such-language-server"]);
    const missing = await lsp.run({
      operation: "diagnostics",
      path: "app.fake",
    });
    expect(missing).toMatchObject({ kind: "done", isError: true });
    expect(missing.kind === "done" && missing.output).toStartWith(
      "unavailable: language server threads-no-such-language-server not started",
    );
    expect(
      await lsp.run({ operation: "symbols", path: "app.rs" }),
    ).toMatchObject({ isError: true });
    expect(
      await lsp.run({ operation: "hover", path: "app.fake" }),
    ).toMatchObject({
      isError: true,
      output: "hover needs line and character",
    });
  });
});

describe("lsp answers", () => {
  test("malformed driver output and unexpected shapes are unavailable", () => {
    expect(answered("hover", "/w/a", "garbage")).toMatchObject({
      isError: true,
    });
    expect(answered("hover", "/w/a", '{"weird": 1}')).toMatchObject({
      isError: true,
    });
    expect(answered("symbols", "/w/a", '{"ok": 5}')).toMatchObject({
      isError: true,
    });
    expect(answered("definition", "/w/a", '{"ok": null}')).toMatchObject({
      isError: false,
      output: "no definition found",
    });
    expect(
      lspText("references", "file:///w/a", [
        {
          targetUri: "file:///w/b",
          targetSelectionRange: {
            start: { line: 1, character: 2 },
            end: { line: 1, character: 3 },
          },
        },
      ]),
    ).toBe("/w/b:2:3");
  });

  test("an unknown language is a setup error; lsp without a sandbox is capability_missing", async () => {
    const model = scriptedModel({ responses: [] });
    await expect(
      agent({
        model,
        sandbox: fakeSandbox(),
        lsp: { languages: ["cobol"] },
      }).check(),
    ).resolves.toMatchObject({
      ok: false,
      error: { code: "unknown_preset" },
    });
    await expect(
      agent({ model, lsp: { languages: ["python"] } }).check(),
    ).resolves.toMatchObject({
      ok: false,
      error: { code: "capability_missing" },
    });
    await expect(
      agent({
        model,
        sandbox: fakeSandbox(),
        lsp: { languages: ["python"] },
      }).check(),
    ).resolves.toEqual({
      ok: true,
      value: undefined,
    });
  });
});
