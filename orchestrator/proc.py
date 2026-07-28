"""Subprocess helper that forces UTF-8, per the project-wide encoding rule.

Used by agents that shell out to external tools (e.g. the Developer agent
running eslint via Node). Plain subprocess.run() without an explicit
encoding decodes output using the OS's preferred encoding, which on
Windows is not UTF-8 and mangles Korean text.
"""

from __future__ import annotations

import os
import subprocess


def run_utf8(
    cmd: list[str],
    cwd: str | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        cmd,
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=timeout,
    )
