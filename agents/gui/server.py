"""Local static HTTP server for serving target-app/ during GUI verification.

Just `python -m http.server` run as a subprocess and polled until it
answers, so the real (system default) browser has something to load.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

DEFAULT_PORT = 8765
READY_TIMEOUT_SECONDS = 10.0
READY_POLL_INTERVAL_SECONDS = 0.2


class LocalStaticServer:
    def __init__(self, directory: Path, port: int = DEFAULT_PORT) -> None:
        self.directory = directory
        self.port = port
        self.url = f"http://127.0.0.1:{port}/"
        self._process: subprocess.Popen[bytes] | None = None

    def start(self, timeout: float = READY_TIMEOUT_SECONDS) -> None:
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._process = subprocess.Popen(
            [sys.executable, "-m", "http.server", str(self.port), "--directory", str(self.directory)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.url, timeout=1)
                return
            except Exception:
                time.sleep(READY_POLL_INTERVAL_SECONDS)

        self.stop()
        raise RuntimeError(f"Local server did not become ready at {self.url} within {timeout}s")

    def stop(self) -> None:
        if self._process is None:
            return
        self._process.terminate()
        try:
            self._process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._process.kill()
        self._process = None

    def __enter__(self) -> "LocalStaticServer":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stop()
