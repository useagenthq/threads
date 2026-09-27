"""Daytona's command log stream: one byte stream where a 3-byte marker switches between stdout
(0x01 x3) and stderr (0x02 x3). Markers can split across frames, so a trailing partial marker is
held back until the next frame.

The markers are in-band and have no escape, so raw output containing them would be misread. The
sandbox base64-encodes each stream before it reaches the channel
(toolbox.PREFIX); base64 never contains a marker byte, and `Unbase64` restores the exact bytes.
"""

import binascii
from dataclasses import dataclass, field
from typing import Literal

STDOUT = b"\x01\x01\x01"
STDERR = b"\x02\x02\x02"
_MARKERS = (STDOUT, STDERR)

type Stream = Literal["stdout", "stderr"]


@dataclass
class Demux:
    stream: Stream = "stdout"
    _held: bytes = field(default=b"")

    def feed(self, data: bytes) -> list[tuple[Stream, bytes]]:
        """The chunks in `data`, each tagged with its stream."""
        buf, out = self._held + data, list[tuple[Stream, bytes]]()
        self._held = b""
        while buf:
            at, marker = _next_marker(buf)
            if marker is None:
                keep = _partial_tail(buf)
                self._emit(out, buf[: len(buf) - keep])
                self._held = buf[len(buf) - keep :]
                break
            self._emit(out, buf[:at])
            self.stream = "stdout" if marker == STDOUT else "stderr"
            buf = buf[at + len(marker) :]
        return out

    def flush(self) -> list[tuple[Stream, bytes]]:
        held, self._held = self._held, b""
        return [(self.stream, held)] if held else []

    def _emit(self, out: list[tuple[Stream, bytes]], chunk: bytes) -> None:
        if chunk:
            out.append((self.stream, chunk))


class Unbase64:
    """One stream's base64 decoded as it arrives: whole 4-character groups at a time, line
    breaks dropped. Anything else is malformed (binascii.Error)."""

    def __init__(self) -> None:
        self._held = b""

    def feed(self, text: bytes) -> bytes:
        buf = self._held + text.translate(None, b"\r\n")
        cut = len(buf) - len(buf) % 4
        self._held = buf[cut:]
        return binascii.a2b_base64(buf[:cut], strict_mode=True)

    def end(self) -> None:
        if self._held:
            raise binascii.Error(f"the stream ended mid-group: {self._held!r}")


def _next_marker(buf: bytes) -> tuple[int, bytes | None]:
    found = [(buf.find(m), m) for m in _MARKERS if buf.find(m) >= 0]
    return min(found) if found else (-1, None)


def _partial_tail(buf: bytes) -> int:
    """How many trailing bytes could start a marker."""
    for size in (2, 1):
        if len(buf) >= size and any(m.startswith(buf[-size:]) for m in _MARKERS):
            return size
    return 0
