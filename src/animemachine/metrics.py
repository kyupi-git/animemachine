"""Small dependency-free timing and progress probes used by startup paths."""
from __future__ import annotations

import contextlib
import sys
import threading
import time
from collections.abc import Iterator
from typing import TextIO


_PROGRESS_LOCK = threading.RLock()
_PROGRESS_WIDTHS: dict[int, int] = {}


def _interactive(stream: TextIO) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError):
        return False


def progress(message: str, *, final: bool = False, stream: TextIO | None = None) -> None:
    """Refresh one TTY line; retain ordinary line-oriented output in logs."""
    target = stream or sys.stdout
    text = str(message)
    if not _interactive(target):
        print(text, file=target, flush=True)
        return
    key = id(target)
    with _PROGRESS_LOCK:
        previous = _PROGRESS_WIDTHS.get(key, 0)
        padding = " " * max(0, previous - len(text))
        target.write(f"\r{text}{padding}")
        if final:
            target.write("\n")
            _PROGRESS_WIDTHS.pop(key, None)
        else:
            _PROGRESS_WIDTHS[key] = len(text)
        target.flush()


def end_progress(stream: TextIO | None = None) -> None:
    """Terminate an active TTY progress line without adding log-only output."""
    target = stream or sys.stdout
    if not _interactive(target):
        return
    key = id(target)
    with _PROGRESS_LOCK:
        if key in _PROGRESS_WIDTHS:
            target.write("\n")
            target.flush()
            _PROGRESS_WIDTHS.pop(key, None)


@contextlib.contextmanager
def stage(name: str) -> Iterator[None]:
    started = time.monotonic()
    try:
        yield
    finally:
        end_progress()
        print(f"[timing] {name}={time.monotonic() - started:.3f}s", flush=True)
