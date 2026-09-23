"""Computer use against a scripted desktop: a screenshot is an image_ref part
with its width and height, each action is one xdotool command, fields that go together are
checked before any effect, and a desktop that doesn't answer is unavailable."""

import asyncio
import struct
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field

import pytest
from sandbox_kit import KitContext

from threads._generated.tools_v1 import ComputerInput, ComputerScreenshotInput
from threads.log import ArtifactRef, ImagePart, JsonObject, TextPart
from threads.log.digest import sha256_hex
from threads.loop.tools import Dispatched, Output, Uncertain
from threads.result import Err, Ok
from threads.sandbox.protocol import ExecResult, SandboxContext, SandboxError, SandboxSession
from threads.tools.desktop import act, invalid_action, invalid_screenshot, png_size, screenshot


def png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, body: bytes) -> bytes:
        crc = zlib.crc32(kind + body)
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IEND", b"")


class _Session:
    def __init__(self, shot: bytes) -> None:
        self.shot = shot

    async def download(self, path: str, context: SandboxContext) -> Ok[bytes] | Err[SandboxError]:
        return Ok(self.shot)


@dataclass
class _Desktop:
    """A `Control` whose commands answer from a script."""

    answer: Ok[ExecResult] | Err[Dispatched]
    shot: bytes = b""
    argv: list[Sequence[str]] = field(default_factory=list[Sequence[str]])
    stored: list[bytes] = field(default_factory=list[bytes])

    @property
    def context(self) -> SandboxContext:
        return KitContext()

    async def session(self) -> SandboxSession | None:
        session: SandboxSession = _Session(self.shot)  # type: ignore[assignment] - download is all a screenshot uses
        return session

    async def put(self, data: bytes, media_type: str) -> ArtifactRef:
        self.stored.append(data)
        return ArtifactRef(sha256=sha256_hex(data), bytes=len(data), media_type=media_type)

    async def command(
        self, argv: Sequence[str], key: str, timeout_ms: int = 0
    ) -> Ok[ExecResult] | Err[Dispatched]:
        self.argv.append(argv)
        return self.answer


def ok(stdout: str = "", code: int = 0, stderr: str = "") -> Ok[ExecResult]:
    return Ok(ExecResult(code, stdout, stderr, False))


def test_a_screenshot_is_an_image_ref_part_with_its_size() -> None:
    desktop = _Desktop(ok("x:640 y:400 screen:0 window:1"), png(1280, 800))
    args = ComputerScreenshotInput.model_validate({"x": 10, "y": 20, "to_x": 110, "to_y": 70})
    got = asyncio.run(screenshot(desktop, args, "b:c1"))
    assert isinstance(got, Output)
    text, image = got.content
    assert isinstance(text, TextPart)
    assert text.text == "Screenshot 1280x800; cursor x:640 y:400 screen:0 window:1."
    assert isinstance(image, ImagePart)
    assert (image.width, image.height, image.ref.media_type) == (1280, 800, "image/png")
    assert desktop.stored == [png(1280, 800)]
    # The zoom region reaches both capture tools: scrot's -a and import's -crop.
    assert list(desktop.argv[0][3:]) == [
        "capture",
        "0",
        "10,20,100,50",
        "/workspace/.threads/screen.png",
        "100x50+10+20",
    ]


def test_a_dead_desktop_is_unavailable_and_a_lost_answer_uncertain() -> None:
    dead = asyncio.run(
        screenshot(_Desktop(ok(code=3, stderr="no display")), ComputerScreenshotInput(), "k")
    )
    assert dead == Output("unavailable: the desktop did not capture: no display", True)
    click = ComputerInput.model_validate({"action": "click", "x": 1, "y": 2})
    lost = asyncio.run(act(_Desktop(Err(Uncertain("timeout"))), click, "k"))
    assert lost == Uncertain("timeout")


@pytest.mark.parametrize(
    ("input", "xdotool"),
    [
        ({"action": "click", "x": 5, "y": 6}, ["mousemove", "5", "6", "click", "1"]),
        (
            {"action": "double_click", "x": 5, "y": 6},
            ["mousemove", "5", "6", "click", "--repeat", "2", "1"],
        ),
        ({"action": "right_click", "x": 5, "y": 6}, ["mousemove", "5", "6", "click", "3"]),
        ({"action": "move", "x": 5, "y": 6}, ["mousemove", "5", "6"]),
        (
            {"action": "drag", "x": 1, "y": 2, "to_x": 3, "to_y": 4},
            ["mousemove", "1", "2", "mousedown", "1", "mousemove", "3", "4", "mouseup", "1"],
        ),
        (
            {"action": "scroll", "x": 1, "y": 2, "direction": "down", "amount": 2},
            ["mousemove", "1", "2", "click", "--repeat", "2", "5"],
        ),
        ({"action": "type", "text": "-rf hi"}, ["type", "--delay", "12", "--", "-rf hi"]),
        ({"action": "key", "text": "ctrl+s"}, ["key", "--", "ctrl+s"]),
    ],
)
def test_each_action_is_one_xdotool_command(input: JsonObject, xdotool: list[str]) -> None:
    desktop = _Desktop(ok())
    args = ComputerInput.model_validate(input)
    assert invalid_action(args) is None
    got = asyncio.run(act(desktop, args, "k"))
    assert isinstance(got, Output)
    assert not got.is_error
    assert list(desktop.argv[0][4:]) == xdotool


@pytest.mark.parametrize(
    ("input", "why"),
    [
        ({"action": "click"}, "click needs x and y"),
        ({"action": "drag", "x": 1, "y": 1}, "drag needs to_x and to_y"),
        ({"action": "type"}, "type needs text"),
        ({"action": "scroll", "x": 1, "y": 1}, "scroll needs direction"),
    ],
)
def test_actions_missing_fields_are_invalid(input: JsonObject, why: str) -> None:
    assert invalid_action(ComputerInput.model_validate(input)) == why


def test_zoom_needs_all_four_corners_in_order() -> None:
    assert invalid_screenshot(ComputerScreenshotInput.model_validate({"x": 1})) is not None
    bad = {"x": 5, "y": 5, "to_x": 5, "to_y": 9}
    assert invalid_screenshot(ComputerScreenshotInput.model_validate(bad)) == "zoom needs to_x > x"
    assert invalid_screenshot(ComputerScreenshotInput()) is None
    assert png_size(b"not a png") is None
