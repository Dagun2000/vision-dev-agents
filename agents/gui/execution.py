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
import re
import shutil
import subprocess
import time
import webbrowser
import winreg
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


def _prog_id_command(prog_id: str) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"{prog_id}\shell\open\command") as key:
            command, _ = winreg.QueryValueEx(key, None)
            return command
    except OSError:
        return None


def _default_browser_exe() -> str | None:
    """Resolve a browser executable path from the registry, so it can be
    launched directly with --new-window (see _open_new_window) instead of
    going through webbrowser.open(), which only *hints* at a new window --
    Chromium browsers often ignore that hint and add a tab to whatever
    window is already open instead. That matters here because if the
    caller's own dashboard is *also* a browser tab (e.g. a Streamlit UI
    open in the same browser), a reused window means closing "the app
    window" after verification closes the dashboard's tab/window too.

    Prefers Chrome (ChromeHTML's ProgId) over whatever's set as the OS
    default: this project's own dashboard is commonly run in Chrome, and
    popping a different browser (e.g. Edge) for the target app, while
    functionally fine, is a jarring mismatch for no benefit. Falls back to
    the actual OS-registered default if Chrome isn't installed.
    """
    command = _prog_id_command("ChromeHTML")
    if command is None:
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\Shell\Associations\UrlAssociations\https\UserChoice",
            ) as key:
                prog_id, _ = winreg.QueryValueEx(key, "ProgId")
            command = _prog_id_command(prog_id)
        except OSError:
            return None
    if command is None:
        return None

    match = re.match(r'"([^"]+)"', command)
    return match.group(1) if match else command.split()[0]


def _open_new_window(url: str) -> bool:
    """Launch the default browser directly with --new-window (a real
    Chromium/Firefox flag), guaranteeing a separate top-level window rather
    than hoping webbrowser.open() produces one. Returns False (falls back
    to webbrowser.open()) if the browser couldn't be resolved or launched."""
    exe_path = _default_browser_exe()
    if exe_path is None:
        return False
    try:
        subprocess.Popen([exe_path, "--new-window", url])
        return True
    except OSError as exc:
        logger.warning("GUI: could not launch %s directly (%s)", exe_path, exc)
        return False


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
    if not _open_new_window(url):
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


def open_new_tab(window: BrowserWindow, url: str) -> bool:
    """Reuse an already-open window by opening `url` in a new tab of it,
    instead of opening a whole new window. Callers that launch the app
    repeatedly (once per Phase, or per GUI-retry within a Phase) should
    use this after the first open_browser_maximized() call -- otherwise
    every call spawns another separate top-level window (guaranteed
    distinct since open_browser_maximized() forces --new-window) and none
    of the earlier ones ever get closed until the whole pipeline run ends,
    piling up open windows. Tabs piling up in the *one* window is fine --
    close_window() at the end takes all of them with it.

    Launches the browser executable directly on `url` (no --new-window),
    the same way open_browser_maximized() launches the *first* one --
    Chrome's single-instance behavior routes a plain `chrome.exe <url>`
    call to a new tab in the existing window rather than a new window.
    Deliberately does *not* use keystrokes (Ctrl+T/Ctrl+L + typing) the
    way an earlier version of this function did: confirmed by direct
    testing that simulating Ctrl+T while the window is fullscreen doesn't
    reliably focus the new tab's address bar (the typed URL went nowhere,
    leaving Chrome's New Tab Page instead of navigating) unless fullscreen
    was toggled off and back on for the keystrokes -- which is exactly the
    visible flicker this avoids by not using keystrokes to navigate at
    all. This mirrors how the *original* open_browser_maximized() (via
    webbrowser.open()) always worked: no keystroke simulation, no
    fullscreen side effects.

    Returns False (caller should fall back to open_browser_maximized()) if
    the window no longer exists -- e.g. the user closed it by hand.
    """
    if _find_live_window(window) is None:
        return False

    exe_path = _default_browser_exe()
    if exe_path is None:
        return False
    try:
        subprocess.Popen([exe_path, url])
    except OSError as exc:
        logger.warning("GUI: could not open new tab via %s (%s)", exe_path, exc)
        return False
    time.sleep(RELOAD_WAIT_SECONDS)
    return True


NATIVE_APP_OPEN_WAIT_SECONDS = 2.0


def open_native_app_maximized(
    command: list[str], cwd: str | None = None, env: dict[str, str] | None = None
) -> BrowserWindow:
    """Launch `command` as a subprocess (a native EXE or an Electron binary
    invocation) and find/activate/maximize the window it opens.

    Deliberately reuses the same window-diffing approach as
    open_browser_maximized() (so it's just as robust to focus-stealing/slow
    startup) but skips everything browser-specific -- no F11 fullscreen
    toggle, no Ctrl+R reload, no address bar. Native apps and Electron
    windows have none of that browser chrome to rely on, and Electron in
    particular doesn't show an address bar by default at all.

    Returns a BrowserWindow (the name is legacy from when this layer was
    browser-only; the dataclass itself is just "a window's region + hwnd",
    equally valid for any top-level window).
    """
    existing_handles = _window_handles()
    # subprocess.Popen() without shell=True can't resolve Windows .cmd/.bat
    # shims (npx, npm, yarn, ...) by bare name -- resolve the real path via
    # PATH first, same fix as the Developer agent's eslint invocation.
    resolved = shutil.which(command[0]) or command[0]
    try:
        subprocess.Popen([resolved, *command[1:]], cwd=cwd, env=env)
    except OSError as exc:
        logger.warning("GUI: could not launch native app %r (%s)", command, exc)
        screen_width, screen_height = pyautogui.size()
        return BrowserWindow(left=0, top=0, width=screen_width, height=screen_height)

    time.sleep(NATIVE_APP_OPEN_WAIT_SECONDS)
    window = _wait_for_new_window(existing_handles)
    if window is None:
        logger.warning("GUI: no newly-opened native app window detected, falling back to getActiveWindow()")
        window = gw.getActiveWindow()

    if window is None:
        logger.warning("GUI: could not detect native app window, falling back to full screen")
        screen_width, screen_height = pyautogui.size()
        return BrowserWindow(left=0, top=0, width=screen_width, height=screen_height)

    _activate(window)
    try:
        if not window.isMaximized:
            window.maximize()
        time.sleep(MAXIMIZE_WAIT_SECONDS)
    except Exception as exc:
        logger.warning("GUI: could not maximize native app window (%s)", exc)

    return BrowserWindow(
        left=window.left,
        top=window.top,
        width=window.width,
        height=window.height,
        hwnd=window._hWnd,
    )


ACTIVATE_SETTLE_SECONDS = 0.2


def _activate(window) -> None:
    try:
        # Windows can silently *refuse* SetForegroundWindow() (which
        # pygetwindow's .activate() wraps) when it's requested by a
        # process/thread not tied to the user's most recent input --
        # normally rare for a foreground CLI script, but now a real risk
        # since the pipeline can run from a background thread (the
        # Streamlit dashboard). AllowSetForegroundWindow(ASFW_ANY) grants
        # blanket permission for the *next* such request to actually
        # succeed instead of being silently dropped. Confirmed as the
        # likely cause of a real bug: Ctrl+L (meant for the address bar)
        # landing on the page's own focused input instead, because the
        # window activation never really took effect.
        ctypes.windll.user32.AllowSetForegroundWindow(-1)  # ASFW_ANY
        window.activate()
        time.sleep(ACTIVATE_SETTLE_SECONDS)
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
    """Screenshot `window`'s region. Re-activates the window by hwnd first:
    pyautogui.screenshot() captures whatever pixels are physically at that
    screen region regardless of focus, so if something else (e.g. the user
    switching to another app while an LLM call was in flight) ended up on
    top of our window, a stale `window.region` would otherwise silently
    screenshot the wrong app -- confirmed happening in practice, not just
    a theoretical risk.
    """
    _reactivate_if_possible(window)
    return pyautogui.screenshot(region=window.region)


def click_at(window: BrowserWindow, local_x: int, local_y: int) -> None:
    """Click a point given in screenshot-local coordinates (i.e. relative
    to `window`'s top-left, exactly what BoundingBox.center gives you).
    Re-activates the window first for the same reason as capture_screenshot."""
    _reactivate_if_possible(window)
    pyautogui.click(window.left + local_x, window.top + local_y)
    time.sleep(CLICK_SETTLE_SECONDS)


def _reactivate_if_possible(window: BrowserWindow) -> None:
    live_window = _find_live_window(window)
    if live_window is not None:
        _activate(live_window)


def _find_live_window(window: BrowserWindow):
    """Look up the live pygetwindow object for `window`'s hwnd -- its own
    left/top/width/height are a snapshot, but a few operations (closing,
    re-activating before typing) need the actual current window object."""
    if window.hwnd is None:
        return None
    for candidate in gw.getAllWindows():
        if candidate._hWnd == window.hwnd:
            return candidate
    return None


def close_window(window: BrowserWindow) -> bool:
    """Close the browser window opened by open_browser_maximized(), by
    hwnd. Best-effort -- returns False (never raises) if the window can't
    be found or won't close, so pipeline cleanup can log a warning and
    move on instead of failing the whole run over a leftover window."""
    if window.hwnd is None:
        logger.warning("GUI: no window handle recorded, can't close browser window")
        return False
    try:
        live_window = _find_live_window(window)
        if live_window is not None:
            live_window.close()
            return True
    except Exception as exc:
        logger.warning("GUI: failed to close browser window (%s)", exc)
        return False

    logger.warning("GUI: browser window (hwnd=%s) no longer exists, nothing to close", window.hwnd)
    return False


CLEAR_STORAGE_JS_URL = "javascript:localStorage.clear();location.reload();"
ADDRESS_BAR_WAIT_SECONDS = 0.3
CLEAR_STORAGE_TYPE_INTERVAL = 0.01


def _exit_fullscreen_if_needed(live_window) -> bool:
    if not _is_fullscreen(live_window):
        return False
    _activate(live_window)
    pyautogui.press("f11")
    time.sleep(FULLSCREEN_WAIT_SECONDS)
    return True


def _restore_fullscreen(window: BrowserWindow) -> None:
    live_window = _find_live_window(window)
    if live_window is None:
        return
    _activate(live_window)
    pyautogui.press("f11")
    time.sleep(FULLSCREEN_WAIT_SECONDS)


def _type_into_address_bar(window: BrowserWindow, text: str) -> bool:
    """Type `text` into the address bar (Ctrl+L) and press Enter.

    Confirmed by direct testing: Ctrl+L does *not* reliably reach Chrome's
    address bar while the window is truly fullscreen (F11) and a page
    element (e.g. an input the previous step typed into) already has
    keyboard focus -- the keystrokes go to that page element instead,
    typing `text` into it and submitting whatever form it's in. Works
    every time once out of fullscreen. So: temporarily exit fullscreen for
    the moment of the keystrokes, then restore it -- more reliable than
    trying to blur the page's focus some other way (Escape didn't help).

    Best-effort: returns False (never raises) if the window can't be
    found.
    """
    live_window = _find_live_window(window)
    if live_window is None:
        return False

    was_fullscreen = _exit_fullscreen_if_needed(live_window)
    _activate(live_window)
    pyautogui.hotkey("ctrl", "l")
    time.sleep(ADDRESS_BAR_WAIT_SECONDS)
    pyautogui.typewrite(text, interval=CLEAR_STORAGE_TYPE_INTERVAL)
    pyautogui.press("enter")
    time.sleep(RELOAD_WAIT_SECONDS)
    if was_fullscreen:
        _restore_fullscreen(window)
    return True


def clear_local_storage(window: BrowserWindow) -> bool:
    """Clear the page's localStorage and reload, so no data from a
    previous Phase's manual/automated testing leaks into the next one.

    Done via a `javascript:` URL typed into the address bar, since this
    execution layer is OS-level only (PyAutoGUI) -- there's no DOM/browser
    API here to call localStorage.clear() directly. It must be *typed*
    (pyautogui.typewrite), not pasted: Chrome strips a pasted "javascript:"
    prefix as an anti-self-XSS measure, but doesn't block typed keystrokes.

    Best-effort: returns False (never raises) if the window can't be
    found, so callers can log a warning and continue rather than fail the
    whole verification run over this.
    """
    if not _type_into_address_bar(window, CLEAR_STORAGE_JS_URL):
        logger.warning("GUI: could not find browser window to clear localStorage")
        return False
    return True


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
