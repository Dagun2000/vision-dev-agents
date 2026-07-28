"""Project-wide UTF-8 enforcement helpers.

Rule (applies to every module in this repo, not just this one): all file
I/O, console output, and subprocess calls must force UTF-8 explicitly.
Without this, Windows' default console codepage (cp949/cp1252, not UTF-8)
silently mangles or crashes on Korean text from print()/logging.
"""

from __future__ import annotations

import sys


def ensure_utf8_stdio() -> None:
    """Reconfigure stdout/stderr to UTF-8, if the stream supports it.

    Call this once, as early as possible, in every script/process entry
    point (main.py, scripts/*.py) before any print() or logging call.
    """
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except ValueError:
                pass
