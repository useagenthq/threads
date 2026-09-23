"""Computer use on the sandbox desktop: `computer_screenshot` (read_only) and
`computer` (actions, unguarded). Both drive the desktop's X display from inside the sandbox:
xdotool for input, scrot or ImageMagick `import` for captures, which the E2B Desktop template,
Daytona's computer-use image and the threads Modal desktop image carry. A screenshot is an
`image_ref` part with its width and height; a desktop that doesn't answer is `unavailable`.

ponytail: one exec driver for every provider; add a per-provider driver if a native
computer-use API proves faster than exec.
"""

import struct
from collections.abc import Sequence
from typing import Final

from pydantic.experimental.missing_sentinel import MISSING

from threads._generated.tools_v1 import ComputerInput, ComputerScreenshotInput
from threads.log import ImagePart, ImageRef, ResultPart, TextPart
from threads.loop.tools import Dispatched, NotSent, Output
from threads.redaction import contains_secret
from threads.result import Err
from threads.tools.control import Control

SHOT: Final = "/workspace/.threads/screen.png"
_DISPLAY: Final = 'export DISPLAY="${DISPLAY:-:0}"; '
_CAPTURE: Final = (
    _DISPLAY + '[ "$1" = 0 ] || sleep "$1"; '
    'if command -v scrot >/dev/null; then scrot -o ${2:+-a "$2"} "$3"; '
    'elif command -v import >/dev/null; then import -window root ${4:+-crop "$4"} "$3"; '
    'else echo "no screenshot tool (scrot or import) in the image" >&2; exit 3; fi && '
    "xdotool getmouselocation"
)
_BUTTONS: Final = {"up": "4", "down": "5", "left": "6", "right": "7"}
_POINTED: Final = frozenset({"click", "double_click", "right_click", "move", "drag", "scroll"})


def invalid_screenshot(args: ComputerScreenshotInput) -> str | None:
    corners = (args.x, args.y, args.to_x, args.to_y)
    given = [c for c in corners if c is not MISSING]
    if given and len(given) != len(corners):
        return "zoom needs x, y, to_x and to_y together"
    if isinstance(args.x, int) and isinstance(args.to_x, int) and args.to_x <= args.x:
        return "zoom needs to_x > x"
    if isinstance(args.y, int) and isinstance(args.to_y, int) and args.to_y <= args.y:
        return "zoom needs to_y > y"
    return None


def invalid_action(args: ComputerInput) -> str | None:
    if args.action in _POINTED and (args.x is MISSING or args.y is MISSING):
        return f"{args.action} needs x and y"
    if args.action == "drag" and (args.to_x is MISSING or args.to_y is MISSING):
        return "drag needs to_x and to_y"
    if args.action in ("type", "key") and args.text is MISSING:
        return f"{args.action} needs text"
    if args.action == "scroll" and args.direction is MISSING:
        return "scroll needs direction"
    return None


def png_size(data: bytes) -> tuple[int, int] | None:
    """Width and height from a PNG's IHDR chunk."""
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):  # noqa: PLR2004 - IHDR end
        return None
    width, height = struct.unpack(">II", data[16:24])
    return (width, height) if width and height else None


async def screenshot(tools: Control, args: ComputerScreenshotInput, key: str) -> Dispatched:
    wait = "0" if args.wait_ms is MISSING else f"{args.wait_ms / 1000:g}"
    scrot = crop = ""
    x, y, to_x, to_y = args.x, args.y, args.to_x, args.to_y
    if (
        isinstance(x, int)
        and isinstance(y, int)
        and isinstance(to_x, int)
        and isinstance(to_y, int)
    ):
        w, h = to_x - x, to_y - y
        scrot, crop = f"{x},{y},{w},{h}", f"{w}x{h}+{x}+{y}"
    ran = await tools.command(["bash", "-c", _CAPTURE, "capture", wait, scrot, SHOT, crop], key)
    if isinstance(ran, Err):
        return ran.error
    if ran.value.exit_code != 0:
        return Output(f"unavailable: the desktop did not capture: {ran.value.stderr.strip()}", True)
    session = await tools.session()
    if session is None:
        return NotSent()
    got = await session.download(SHOT, tools.context)
    size = None if isinstance(got, Err) else png_size(got.value)
    if isinstance(got, Err) or size is None:
        return Output("unavailable: the screenshot is not a PNG", True)
    if contains_secret(got.value):
        # Byte-exact: an image holding a registered value (a text chunk) is refused, never stored.
        return Output("refused: the capture holds a registered secret; not stored", True)
    ref = await tools.put(got.value, "image/png")
    image = ImageRef(sha256=ref.sha256, bytes=ref.bytes, media_type="image/png")
    text = f"Screenshot {size[0]}x{size[1]}; cursor {ran.value.stdout.strip()}."
    parts: tuple[ResultPart, ...] = (
        TextPart(type="text", text=text),
        ImagePart(type="image_ref", ref=image, width=size[0], height=size[1]),
    )
    return Output(text, content=parts)


def _xdotool(args: ComputerInput) -> Sequence[str]:  # noqa: PLR0911 - one per action
    at = ["mousemove", str(args.x), str(args.y)]
    match args.action:
        case "click":
            return [*at, "click", "1"]
        case "double_click":
            return [*at, "click", "--repeat", "2", "1"]
        case "right_click":
            return [*at, "click", "3"]
        case "move":
            return at
        case "drag":
            end = ["mousemove", str(args.to_x), str(args.to_y)]
            return [*at, "mousedown", "1", *end, "mouseup", "1"]
        case "scroll":
            amount = "3" if args.amount is MISSING else str(args.amount)
            button = _BUTTONS[str(args.direction)]
            return [*at, "click", "--repeat", amount, button]
        case "type":
            return ["type", "--delay", "12", "--", str(args.text)]
        case "key":
            return ["key", "--", str(args.text)]


async def act(tools: Control, args: ComputerInput, key: str) -> Dispatched:
    """An action is unguarded: a lost answer is uncertainty that parks, never a retry."""
    argv = ["bash", "-c", _DISPLAY + 'xdotool "$@"', "act", *_xdotool(args)]
    ran = await tools.command(argv, key)
    if isinstance(ran, Err):
        return ran.error
    if ran.value.exit_code != 0:
        return Output(f"unavailable: the desktop did not act: {ran.value.stderr.strip()}", True)
    where = "" if args.x is MISSING else f" at ({args.x}, {args.y})"
    return Output(f"{args.action}{where} done; take a screenshot to see the result.")
