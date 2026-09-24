# pyright: strict
"""The E2B wire vector's envd cases: the `Start` stream, the unary `SendSignal` and the
`/files` API (e2b_wire.py cites the sources)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from .e2b_wire_pieces import CAP, DOMAIN, SBX, TOKEN, case, reply

if TYPE_CHECKING:
    from .jcs import JsonValue, Obj

ENVD = f"https://49983-{SBX}.{DOMAIN}"
ENVD_HEADERS: Obj = {
    "e2b-sandbox-id": SBX,
    "e2b-sandbox-port": "49983",
    "authorization": "Basic cm9vdDo=",
    "x-access-token": TOKEN,
}
STREAM_HEADERS: Obj = {
    **ENVD_HEADERS,
    "keepalive-ping-interval": "50",
    "connect-protocol-version": "1",
    "content-type": "application/connect+json",
}
UNARY_HEADERS: Obj = {
    **ENVD_HEADERS,
    "connect-protocol-version": "1",
    "content-type": "application/json",
}


def _envelope(flags: int, message: JsonValue) -> bytes:
    body = json.dumps(message, separators=(",", ":")).encode()
    return bytes([flags]) + len(body).to_bytes(4, "big") + body


def _stream(*frames: bytes) -> Obj:
    headers: Obj = {"content-type": "application/connect+json"}
    return {"status": 200, "headers": headers, "hex": b"".join(frames).hex()}


def _event(event: Obj) -> bytes:
    return _envelope(0, {"event": event})


END_OK = _envelope(2, {})
STARTED = _event({"start": {"pid": 7}})


def start_cases() -> list[Obj]:
    call: Obj = {
        "op": "start",
        "argv": ["/bin/sh", "-c", "echo hi"],
        "env": {},
        "cwd": "/",
        "tag": "pk-1",
    }
    sent: Obj = {
        "process": {"cmd": "/bin/sh", "args": ["-c", "echo hi"], "cwd": "/"},
        "tag": "pk-1",
    }
    req: Obj = {
        "method": "POST",
        "url": f"{ENVD}/process.Process/Start",
        "headers": STREAM_HEADERS,
        "envelope": sent,
    }
    with_env: Obj = {
        "op": "start",
        "argv": ["env"],
        "env": {"A": "1"},
        "cwd": "/workspace",
        "tag": None,
    }
    # Protobuf JSON leaves out empty fields: no args, and no tag when none is given.
    env_sent: Obj = {"process": {"cmd": "env", "envs": {"A": "1"}, "cwd": "/workspace"}}
    env_req: Obj = {**req, "envelope": env_sent}
    out = _event({"data": {"stdout": "aGkK"}})
    err = _event({"data": {"stderr": "b29wcw=="}})
    end3 = _event({"end": {"exitCode": 3, "exited": True, "status": "exit status 3", "later": 1}})
    end0 = _event({"end": {"exited": True, "status": "exit status 0"}})
    failed = _envelope(2, {"error": {"code": "unavailable", "message": "envd went away"}})
    both = _event({"data": {"stdout": "aGkK", "stderr": "aGkK"}})
    oversize = bytes([0]) + (CAP + 1).to_bytes(4, "big")
    lost: Obj = {"stdout": "hi\n", "stderr": "", "exit_error": "unavailable"}
    rows: list[tuple[str, Obj, Obj, Obj]] = [
        (
            "start",
            req,
            _stream(STARTED, out, _event({"keepalive": {}}), err, end3, END_OK),
            {"ok": {"stdout": "hi\n", "stderr": "oops", "exit": 3}},
        ),
        (
            "start_with_env_and_no_tag",
            env_req,
            _stream(STARTED, end0, END_OK),
            {"ok": {"stdout": "", "stderr": "", "exit": 0}},
        ),
        (
            "start_output_before_start",
            req,
            _stream(out, STARTED, end0, END_OK),
            {"ok": {"stdout": "hi\n", "stderr": "", "exit": 0}},
        ),
        ("start_end_stream_error", req, _stream(failed), {"error": "unavailable"}),
        ("start_error_after_start", req, _stream(STARTED, out, failed), {"ok": lost}),
        ("start_stream_without_end", req, _stream(STARTED, out, END_OK), {"ok": lost}),
        (
            "start_truncated_frame",
            req,
            _stream(STARTED, out, _event({"end": {"exited": True}})[:-3]),
            {"ok": lost},
        ),
        ("start_compressed_flag", req, _stream(bytes([1]) + STARTED[1:]), {"error": "unavailable"}),
        ("start_oversize_message", req, _stream(oversize), {"error": "unavailable"}),
        (
            "start_two_outputs_in_one_event",
            req,
            _stream(STARTED, both, end0, END_OK),
            {"ok": {"stdout": "", "stderr": "", "exit_error": "unavailable"}},
        ),
        (
            "start_http_error",
            req,
            reply(503, {"code": "unavailable", "message": "no envd"}),
            {"error": "unavailable"},
        ),
    ]
    return [
        case(
            name,
            with_env if name == "start_with_env_and_no_tag" else call,
            request,
            response,
            expect,
        )
        for name, request, response, expect in rows
    ]


def signal_cases() -> list[Obj]:
    call: Obj = {"op": "signal", "tag": "pk-1"}
    sent: Obj = {"process": {"tag": "pk-1"}, "signal": "SIGNAL_SIGKILL"}
    req: Obj = {
        "method": "POST",
        "url": f"{ENVD}/process.Process/SendSignal",
        "headers": UNARY_HEADERS,
        "json": sent,
    }
    return [
        case("signal", call, req, reply(200, {}), {"ok": True}),
        case(
            "signal_no_such_process",
            call,
            req,
            reply(404, {"code": "not_found", "message": "no process"}),
            {"ok": False},
        ),
        case(
            "signal_error",
            call,
            req,
            reply(500, {"code": "internal", "message": "boom"}),
            {"error": "unavailable"},
        ),
    ]


def file_cases() -> list[Obj]:
    path = "/workspace/a b.txt"
    url = f"{ENVD}/files?path=%2Fworkspace%2Fa+b.txt&username=root"
    upload: Obj = {"op": "upload", "path": path, "text": "abc"}
    download: Obj = {"op": "download", "path": path}
    post: Obj = {
        "method": "POST",
        "url": url,
        "headers": ENVD_HEADERS,
        "multipart": {"name": "file", "filename": path, "text": "abc"},
    }
    get: Obj = {"method": "GET", "url": url, "headers": ENVD_HEADERS}
    listing: JsonValue = [{"name": "a b.txt", "type": "file", "path": path}]

    def failed(status: int, message: str, code: str) -> Obj:
        return case(
            f"download_{status}_{code}",
            download,
            get,
            reply(status, {"code": status, "message": message}),
            {"error": code},
        )

    return [
        case("upload", upload, post, reply(200, listing), {"ok": None}),
        case(
            "upload_too_large",
            upload,
            post,
            reply(507, {"code": 507, "message": "no space"}),
            {"error": "too_large"},
        ),
        case(
            "download",
            download,
            get,
            {"status": 200, "headers": {"content-type": "application/octet-stream"}, "text": "abc"},
            {"ok": "abc"},
        ),
        failed(404, "no such file", "not_found"),
        failed(400, "path is a directory", "is_directory"),
        failed(400, "bad path", "invalid_path"),
        failed(401, "denied", "permission_denied"),
        failed(413, "too big", "too_large"),
        failed(502, "gone", "unavailable"),
    ]
