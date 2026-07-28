"""Clear target-app's localStorage for real, by actually opening it in a
browser and running the same clear_local_storage() sequence the GUI Tester
uses before every verify_phase() call.

localStorage lives in the browser (keyed by origin), not in any repo file --
closing/reopening the browser window does NOT clear it, and neither does
resetting state/plan.json or target-app/*.py. This is the only way to
actually reset it short of clearing it by hand in DevTools.

If a browser window showing the app is *already* open, this reuses that
exact window (matched by its title, read from target-app/index.html's
<title>) instead of asking the OS to open a "new" one -- webbrowser.open()
often just reuses the already-running browser's window/tab instead of
opening a genuinely new one, which left agents/gui/execution.py's new-
window-detection falling back to getActiveWindow() and silently operating
on the wrong window (confirmed: it kept closing some unrelated window while
the real one, with the stale Todo items, sat untouched every time).

Run: uv run python scripts/reset_local_storage.py
(Takes over the mouse/keyboard briefly via PyAutoGUI -- don't touch either
while it runs.)
"""

from __future__ import annotations

import json
import re
import time

import pygetwindow as gw

from agents.gui.execution import (
    BrowserWindow,
    clear_local_storage,
    close_window,
    enable_windows_dpi_awareness,
    open_browser_maximized,
)
from agents.gui.server import LocalStaticServer
from orchestrator.config import PipelineConfig

DEFAULT_PORT = 8000  # matches the port the Developer agent's LLM output has
# consistently used so far (agents/developer.py's SYSTEM_PROMPT example) --
# not a hardcoded protocol, just today's de facto convention.


def _last_used_port(config: PipelineConfig) -> int:
    for candidate in (config.plan_file, config.plan_file.with_suffix(".json.bak")):
        if not candidate.exists():
            continue
        try:
            plan = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for phase in plan.get("phases", []):
            launch_config = phase.get("launch_config")
            if launch_config and launch_config.get("entry_url"):
                port = launch_config["entry_url"].split(":")[-1].split("/")[0]
                if port.isdigit():
                    return int(port)
    return DEFAULT_PORT


def _page_title(config: PipelineConfig) -> str | None:
    index_html = config.target_app_dir / "index.html"
    if not index_html.exists():
        return None
    match = re.search(r"<title>(.*?)</title>", index_html.read_text(encoding="utf-8"), re.IGNORECASE)
    return match.group(1).strip() if match else None


def _find_open_app_window(page_title: str | None):
    """A browser tab's OS window title is "<page title> - <Browser>". Match
    on that rather than trusting webbrowser.open() to hand us a fresh
    window -- see the module docstring for why."""
    if not page_title:
        return None
    for window in gw.getAllWindows():
        title = window.title.strip()
        if title and title.lower().startswith(page_title.lower()):
            return window
    return None


def main() -> None:
    config = PipelineConfig()
    enable_windows_dpi_awareness()

    port = _last_used_port(config)
    server = LocalStaticServer(config.target_app_dir, port=port)
    server.start()
    print(f"server up at {server.url}")

    try:
        existing = _find_open_app_window(_page_title(config))
        if existing is not None:
            print(f"reusing already-open window: {existing.title!r}")
            window = BrowserWindow(
                left=existing.left,
                top=existing.top,
                width=existing.width,
                height=existing.height,
                hwnd=existing._hWnd,
            )
        else:
            print("no matching window open -- opening a fresh one")
            window = open_browser_maximized(server.url + "index.html")
            time.sleep(1.0)

        cleared = clear_local_storage(window)
        print("clear_local_storage() ->", cleared)
        time.sleep(1.0)
        closed = close_window(window)
        print("close_window() ->", closed)
    finally:
        server.stop()
        print("server stopped")


if __name__ == "__main__":
    main()
