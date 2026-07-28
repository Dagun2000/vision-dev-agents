"""OS-level execution layer for the GUI Tester agent.

Everything here drives the *real* screen via PyAutoGUI / OS window APIs --
opening the system's default browser, capturing screenshots, and (in a
later step) performing clicks/keystrokes. This module must never import
anything DOM- or browser-API-flavored (no Selenium/Playwright/CDP): the
judgment side (agents/gui/judgment.py, added in a later step) decides
*what* to click purely from screenshots + the Vision model. This module
only knows how to *do* it.
"""

from __future__ import annotations

import ctypes
import logging
import time
import webbrowser
from dataclasses import dataclass

import pyautogui
import pygetwindow as gw
import pyperclip
from PIL import Image

logger = logging.getLogger("pipeline")

BROWSER_OPEN_WAIT_SECONDS = 1.5
NEW_WINDOW_POLL_INTERVAL_SECONDS = 0.3
NEW_WINDOW_POLL_TIMEOUT_SECONDS = 5.0
MAXIMIZE_WAIT_SECONDS = 0.5
# Chrome/Edge show a transient "Esc를 눌러 전체화면 종료" overlay banner for a
# few seconds after entering fullscreen, unless the user has disabled it
# (e.g. via chrome://flags or an OS-level setting). If that banner shows up
# in screenshots on a given machine, bump this back up:
# FULLSCREEN_WAIT_SECONDS = 5.0
FULLSCREEN_WAIT_SECONDS = 1.0
RELOAD_WAIT_SECONDS = 0.8
CLICK_SETTLE_SECONDS = 0.15
PASTE_SETTLE_SECONDS = 0.1


def enable_windows_dpi_awareness() -> None:
    """Make window/screen coordinates match physical pixels.

    Without this, Windows may report scaled ("logical") coordinates that
    don't line up with what PyAutoGUI's screenshot/click calls actually
    use, causing click coordinates to drift on scaled displays. Safe to
    call more than once (a second call just fails quietly).
    """
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError) as exc:
        logger.warning("GUI: could not set DPI awareness (%s) -- already set, or not Windows?", exc)


@dataclass
class BrowserWindow:
    left: int
    top: int
    width: int
    height: int
    hwnd: int | None = None

    @property
    def region(self) -> tuple[int, int, int, int]:
        """(left, top, width, height), the shape pyautogui.screenshot() expects."""
        return (self.left, self.top, self.width, self.height)


def _window_handles() -> set[int]:
    return {w._hWnd for w in gw.getAllWindows()}


def _wait_for_new_window(existing_handles: set[int]):
    """Poll for a window that didn't exist in `existing_handles` before we
    called webbrowser.open(). Diffing window handles (rather than trusting
    getActiveWindow()) is what makes this robust even if the new browser
    window doesn't actually take OS focus -- e.g. because the user is
    doing something else on their machine, or Windows' focus-stealing
    prevention kicks in after many windows have already been opened during
    a long session. Returns None if no new window shows up in time."""
    deadline = time.monotonic() + NEW_WINDOW_POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        for window in gw.getAllWindows():
            if window._hWnd not in existing_handles and window.title.strip():
                return window
        time.sleep(NEW_WINDOW_POLL_INTERVAL_SECONDS)
    return None


def open_browser_maximized(url: str) -> BrowserWindow:
    """Open `url` in the system default browser, bring it to the
    foreground, maximize it, and try to drop into fullscreen (F11) so its
    own chrome (tabs/address bar) is hidden and screenshots show just page
    content. Returns the resulting window's screen region, used later for
    cropped screenshots.

    Explicitly finds and activates the newly-opened window rather than
    assuming it already has focus -- webbrowser.open() doesn't guarantee
    that, especially with other windows already open.
    """
    existing_handles = _window_handles()
    webbrowser.open(url, new=1)
    time.sleep(BROWSER_OPEN_WAIT_SECONDS)

    window = _wait_for_new_window(existing_handles)
    if window is None:
        logger.warning("GUI: no newly-opened window detected, falling back to getActiveWindow()")
        window = gw.getActiveWindow()

    if window is None:
        logger.warning("GUI: could not detect browser window, falling back to full screen")
        screen_width, screen_height = pyautogui.size()
        return BrowserWindow(left=0, top=0, width=screen_width, height=screen_height)

    # Re-activate `window` (by hwnd, not "whatever's currently focused")
    # before every keystroke below -- something else on the machine could
    # have stolen focus during any of the sleeps in between.
    _activate(window)
    try:
        if not window.isMaximized:
            window.maximize()
        time.sleep(MAXIMIZE_WAIT_SECONDS)
    except Exception as exc:
        logger.warning("GUI: could not maximize browser window (%s)", exc)

    # Best-effort: F11 is a plain keystroke (not a browser API call), so
    # this stays within the "no DOM/browser API" rule -- it just hides
    # the browser's own tabs/address bar the way a user would. F11
    # *toggles* fullscreen, though -- if a previous run already left this
    # window fullscreen (e.g. the browser reused the same window for a
    # new tab), pressing it again would exit fullscreen instead. Only
    # press it if the window isn't already covering the full screen.
    if not _is_fullscreen(window):
        _activate(window)
        pyautogui.press("f11")
        time.sleep(FULLSCREEN_WAIT_SECONDS)
    else:
        logger.info("GUI: window already fullscreen, skipping F11")

    # Force a hard reload: if the browser reused an already-open tab on
    # this URL instead of a truly fresh one, that tab's in-page JS state
    # (e.g. Todo items typed in during manual testing) would otherwise
    # still be sitting there. A reload re-runs app.js from scratch.
    _activate(window)
    pyautogui.hotkey("ctrl", "r")
    time.sleep(RELOAD_WAIT_SECONDS)

    # `window`'s properties query the live window (by hwnd) each access,
    # so this reflects its current geometry regardless of who has focus
    # right now -- unlike gw.getActiveWindow(), which would return
    # whatever unrelated window the user's since clicked into.
    return BrowserWindow(
        left=window.left,
        top=window.top,
        width=window.width,
        height=window.height,
        hwnd=window._hWnd,
    )


def _activate(window) -> None:
    try:
        window.activate()
    except Exception as exc:
        logger.warning("GUI: could not activate browser window (%s)", exc)


def _is_fullscreen(window) -> bool:
    """True Windows fullscreen covers the whole monitor (including where
    the taskbar would be); merely "maximized" is sized to the work area
    (screen minus taskbar), which is smaller. That gap is what lets us
    tell the two apart without any DOM/browser-specific query."""
    screen_width, screen_height = pyautogui.size()
    return window.left <= 0 and window.top <= 0 and window.width >= screen_width and window.height >= screen_height


def exit_fullscreen() -> None:
    pyautogui.press("f11")


def capture_screenshot(window: BrowserWindow) -> Image.Image:
    return pyautogui.screenshot(region=window.region)


def click_at(window: BrowserWindow, local_x: int, local_y: int) -> None:
    """Click a point given in screenshot-local coordinates (i.e. relative
    to `window`'s top-left, exactly what BoundingBox.center gives you)."""
    pyautogui.click(window.left + local_x, window.top + local_y)
    time.sleep(CLICK_SETTLE_SECONDS)


def close_window(window: BrowserWindow) -> bool:
    """Close the browser window opened by open_browser_maximized(), by
    hwnd. Best-effort -- returns False (never raises) if the window can't
    be found or won't close, so pipeline cleanup can log a warning and
    move on instead of failing the whole run over a leftover window."""
    if window.hwnd is None:
        logger.warning("GUI: no window handle recorded, can't close browser window")
        return False
    try:
        for candidate in gw.getAllWindows():
            if candidate._hWnd == window.hwnd:
                candidate.close()
                return True
    except Exception as exc:
        logger.warning("GUI: failed to close browser window (%s)", exc)
        return False

    logger.warning("GUI: browser window (hwnd=%s) no longer exists, nothing to close", window.hwnd)
    return False


def type_text(text: str) -> None:
    """Type `text` into whatever currently has focus.

    Uses clipboard paste (Ctrl+V) rather than pyautogui.typewrite()/write():
    typewrite() simulates individual keystrokes from a fixed US-keyboard
    layout, which can't produce Korean (or other IME-composed) text --
    exactly what this project's success_criteria are written in. Clipboard
    paste sidesteps keystroke simulation entirely, so it's Unicode-safe.
    """
    previous_clipboard = pyperclip.paste()
    try:
        pyperclip.copy(text)
        time.sleep(PASTE_SETTLE_SECONDS)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(PASTE_SETTLE_SECONDS)
    finally:
        pyperclip.copy(previous_clipboard)
