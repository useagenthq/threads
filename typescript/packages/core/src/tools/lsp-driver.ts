// The in-sandbox LSP client lsp runs with `python3 -c`: it starts the
// image's language server, opens one file, asks one question and prints one JSON line:
// {"ok": <LSP result>} or {"unavailable": <why>}. Stdlib only, so any image with python3 and the
// server answers the same way. It answers the server's own requests with empty results.

/** Where the driver looks up a bare server command, and the server's whole PATH. */
export const LSP_SEARCH_PATH =
  "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin";

export const LSP_DRIVER: string = String.raw`
import json, os, queue, shutil, subprocess, sys, threading, time

a = json.loads(sys.argv[1])
# The driver runs with the tool env, which has no PATH: the default search (/bin:/usr/bin)
# would miss servers in /usr/local/bin, where pip and npm put them, and a launcher's
# "#!/usr/bin/env node" would find no node. So the server is looked up in, and runs with a PATH
# of, exactly these system dirs: never a host PATH. An absolute command runs as given.
SEARCH = "${LSP_SEARCH_PATH}"
a["command"][0] = shutil.which(a["command"][0], path=SEARCH) or a["command"][0]
server_env = dict(os.environ, PATH=SEARCH)

def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

try:
    proc = subprocess.Popen(a["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, cwd=a["root"], env=server_env)
except OSError as e:
    out({"unavailable": "language server %s not started: %s" % (a["command"][0], e)})
    sys.exit(0)

inbox = queue.Queue()

def reader():
    f = proc.stdout
    while True:
        length = None
        while True:
            line = f.readline()
            if not line:
                inbox.put(None)
                return
            line = line.strip()
            if not line:
                break
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1])
        if length is None:
            continue
        inbox.put(json.loads(f.read(length)))

threading.Thread(target=reader, daemon=True).start()

def send(msg):
    msg["jsonrpc"] = "2.0"
    body = json.dumps(msg).encode()
    proc.stdin.write(b"Content-Length: %d\r\n\r\n" % len(body) + body)
    proc.stdin.flush()

def answer(msg):
    method = msg.get("method")
    if method == "workspace/configuration":
        send({"id": msg["id"], "result": [None for _ in msg["params"].get("items", [])]})
    else:
        send({"id": msg["id"], "result": None})

def wait(pred, seconds):
    end = time.time() + seconds
    while time.time() < end:
        try:
            msg = inbox.get(timeout=max(0.01, end - time.time()))
        except queue.Empty:
            return None
        if msg is None:
            return None
        if "method" in msg and "id" in msg:
            answer(msg)
            continue
        if pred(msg):
            return msg
    return None

def request(id_, method, params, seconds):
    send({"id": id_, "method": method, "params": params})
    return wait(lambda m: m.get("id") == id_ and "method" not in m, seconds)

root_uri = "file://" + a["root"]
uri = "file://" + a["path"]
init = request(1, "initialize", {"processId": os.getpid(), "rootUri": root_uri,
    "workspaceFolders": [{"uri": root_uri, "name": "workspace"}],
    "capabilities": {"textDocument": {"publishDiagnostics": {}, "hover": {"contentFormat": ["plaintext"]},
                     "documentSymbol": {"hierarchicalDocumentSymbolSupport": True}}}}, a["ready_s"])
if init is None or "error" in init:
    out({"unavailable": "language server %s is not ready" % a["command"][0]})
    proc.kill()
    sys.exit(0)
send({"method": "initialized", "params": {}})
with open(a["path"], encoding="utf-8", errors="replace") as fh:
    text = fh.read()
send({"method": "textDocument/didOpen", "params": {"textDocument": {
    "uri": uri, "languageId": a["language_id"], "version": 1, "text": text}}})

op = a["operation"]
if op == "diagnostics":
    got = wait(lambda m: m.get("method") == "textDocument/publishDiagnostics"
               and m["params"]["uri"] == uri, a["wait_s"])
    if got is None:
        out({"unavailable": "no diagnostics were published for the file"})
    else:
        later = wait(lambda m: m.get("method") == "textDocument/publishDiagnostics"
                     and m["params"]["uri"] == uri, 1.0)
        out({"ok": (later or got)["params"]["diagnostics"]})
else:
    pos = {"line": a.get("line", 1) - 1, "character": a.get("character", 1) - 1}
    doc = {"textDocument": {"uri": uri}}
    method, params = {
        "definition": ("textDocument/definition", dict(doc, position=pos)),
        "references": ("textDocument/references", dict(doc, position=pos, context={"includeDeclaration": True})),
        "hover": ("textDocument/hover", dict(doc, position=pos)),
        "symbols": ("textDocument/documentSymbol", doc),
    }[op]
    got = request(2, method, params, a["wait_s"])
    if got is None:
        out({"unavailable": "the language server did not answer %s" % method})
    elif "error" in got:
        out({"unavailable": "the language server refused %s: %s" % (method, got["error"].get("message"))})
    else:
        out({"ok": got.get("result")})
try:
    request(3, "shutdown", None, 2)
    send({"method": "exit"})
except OSError:
    pass
proc.kill()
`;
