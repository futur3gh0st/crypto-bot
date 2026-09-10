"""Non-blocking single-key input for the desk. POSIX raw mode, degrades safely."""

from __future__ import annotations

import asyncio
import contextlib
import os
import sys
from typing import Iterator

try:
    import termios
    import tty

    HAVE_TERMIOS = True
except ImportError:  # pragma: no cover - windows
    HAVE_TERMIOS = False


def stdin_is_tty() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (ValueError, AttributeError):
        return False


@contextlib.contextmanager
def raw_mode() -> Iterator[bool]:
    """Put the terminal in cbreak mode; always restore it, even on a crash."""
    if not (HAVE_TERMIOS and stdin_is_tty()):
        yield False
        return
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield True
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class KeyReader:
    """Reads one keypress at a time without blocking the event loop."""

    def __init__(self) -> None:
        self.enabled = HAVE_TERMIOS and stdin_is_tty()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def _on_readable(self) -> None:
        try:
            ch = os.read(sys.stdin.fileno(), 1)
        except (BlockingIOError, OSError):
            return
        if not ch:
            return
        try:
            self._queue.put_nowait(ch.decode("utf-8", "ignore"))
        except asyncio.QueueFull:  # pragma: no cover
            pass

    def start(self) -> bool:
        if not self.enabled:
            return False
        loop = asyncio.get_running_loop()
        try:
            loop.add_reader(sys.stdin.fileno(), self._on_readable)
        except (NotImplementedError, ValueError):  # pragma: no cover
            self.enabled = False
            return False
        return True

    def stop(self) -> None:
        if not self.enabled:
            return
        loop = asyncio.get_event_loop()
        with contextlib.suppress(Exception):
            loop.remove_reader(sys.stdin.fileno())

    async def get(self, timeout: float = 0.1) -> str | None:
        if not self.enabled:
            await asyncio.sleep(timeout)
            return None
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def drain(self) -> list[str]:
        out: list[str] = []
        while not self._queue.empty():
            with contextlib.suppress(asyncio.QueueEmpty):
                out.append(self._queue.get_nowait())
        return out
