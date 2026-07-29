"""GUI Tester agent implementation.

Scope so far:
- 4-1: serve target-app/ locally, open it in the system's default browser,
  capture a real on-screen screenshot (PyAutoGUI), detect clickable-looking
  elements via OpenCV, and overlay numbered Set-of-marks labels.
- 4-2: send the labeled screenshot (image only -- no separate OCR text
  list) plus the Phase's success_criteria to the Vision model and get back
  a single next-action decision (agents/gui/judgment.py).
- 4-3: execute that action for real via PyAutoGUI (agents/gui/execution.py),
  check whether the screen actually changed, and loop
  capture -> judge -> execute -> check up to MAX_GUI_STEPS times, building
  a step_log and returning a GUITestOutputSchema. Exposed as verify_phase().

Local OCR (agents/gui/ocr.py) is intentionally NOT used here: this is a
GPU-less environment, so per-box OCR was dropped in favor of letting the
Vision model read the screenshot image directly. The module is kept
un-deleted in case local OCR is worth reintroducing later.

verify() (the ABC method the orchestrator calls) is not implemented yet --
that's step 4-4, which will also wire verify_phase()'s result into
plan.json and the Developer feedback loop.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from PIL import Image

from agents.base import GUITesterAgent
from agents.gui.detection import (
    BoundingBox,
    detect_clickable_elements,
    overlay_labels,
    padded_region,
    screenshots_differ,
)
from agents.gui.execution import (
    BrowserWindow,
    capture_screenshot,
    click_at,
    close_window,
    enable_windows_dpi_awareness,
    open_browser_maximized,
    open_native_app_maximized,
    open_new_tab,
    type_text,
)
from agents.gui.judgment import decide_next_action
from agents.gui.server import DEFAULT_PORT, LocalStaticServer
from agents.models import DevResult, GUITestResult, LaunchConfig, LaunchType, Phase
from agents.schemas import GUIActionSchema, GUIStepLogEntry, GUITestOutputSchema
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

MAX_GUI_STEPS = 8
# After this many consecutive ineffective actions (no target found, or the
# screen just didn't change), tell the model explicitly instead of letting
# it keep retrying the same wrong coordinates.
CONSECUTIVE_INEFFECTIVE_LIMIT = 2
ACTION_SETTLE_SECONDS = 0.5
# Margin (px) added around an action's target box when diffing just that
# region -- just enough to catch visual feedback that overflows the box
# slightly (typed text, a checkbox's strikethrough label), without pulling
# in unrelated neighboring elements.
LOCAL_DIFF_PADDING_PX = 6


class OpenAIGUITesterAgent(GUITesterAgent):
    """Named for historical reasons (this whole pipeline's other three
    agents are OpenAI-only) -- its own vision judgment call is actually
    multi-provider, see agents/gui/judgment.py and config.gui_tester_provider."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self._last_window: BrowserWindow | None = None
        self._launch_count = 0
        enable_windows_dpi_awareness()

    def cleanup(self) -> None:
        """Close the browser window left open by the most recent
        verify_phase() call. Best-effort: any failure is logged and
        swallowed, never raised -- this runs after the report is already
        saved, so it must not affect the pipeline's success/failure."""
        if self._last_window is None:
            return
        try:
            closed = close_window(self._last_window)
            if closed:
                logger.info("GUI: closed browser window from last verify_phase() run")
            else:
                logger.warning("GUI: could not close the last browser window (see prior warning)")
        except Exception as exc:
            logger.warning("GUI: cleanup() failed unexpectedly (%s)", exc)
        finally:
            self._last_window = None

    def verify(self, phase: Phase, dev_result: DevResult) -> GUITestResult:
        raise NotImplementedError(
            "OpenAIGUITesterAgent.verify is not implemented yet -- "
            "verify_phase() has the full capture/judge/act loop, but it isn't "
            "wired into the ABC interface / plan.json / Developer feedback yet"
        )

    # ---- step 4-1: capture + detect + label ---------------------------

    def capture_labeled_screenshot(
        self,
    ) -> tuple[Path, Path, list[BoundingBox], Image.Image, BrowserWindow]:
        """Open target-app/ in the real default browser, screenshot it,
        and save both the raw and Set-of-marks-labeled screenshots under
        logs/screenshots/. Returns
        (raw_path, labeled_path, boxes, labeled_image, window)."""
        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)

        with LocalStaticServer(self.config.target_app_dir) as server:
            logger.info("GUI: serving target-app at %s", server.url)
            window = open_browser_maximized(server.url)
            logger.info("GUI: browser window region=%s", window.region)
            screenshot = capture_screenshot(window)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        raw_path = self.config.screenshots_dir / f"{timestamp}_raw.png"
        labeled_path = self.config.screenshots_dir / f"{timestamp}_labeled.png"
        screenshot.save(raw_path)

        boxes = detect_clickable_elements(screenshot)
        labeled_image = overlay_labels(screenshot, boxes)
        labeled_image.save(labeled_path)

        logger.info(
            "GUI: detected %d candidate element(s), saved raw=%s labeled=%s",
            len(boxes),
            raw_path,
            labeled_path,
        )
        return raw_path, labeled_path, boxes, labeled_image, window

    # ---- step 4-2: + Vision judgment of the next action ----------------

    def judge_next_action(
        self, phase: Phase, step_log: list[str] | None = None
    ) -> tuple[GUIActionSchema, Path, Path, list[BoundingBox]]:
        """Capture+label a screenshot and ask the Vision model what to do
        next given phase.success_criteria. Returns
        (action, raw_path, labeled_path, boxes) -- nothing is clicked/typed
        yet. Kept as a standalone single-shot method for testing; the real
        loop is verify_phase()."""
        raw_path, labeled_path, boxes, labeled_image, _window = self.capture_labeled_screenshot()

        action = decide_next_action(
            config=self.config,
            labeled_screenshot=labeled_image,
            success_criteria=phase.success_criteria,
            step_log=step_log,
        )
        return action, raw_path, labeled_path, boxes

    # ---- launch dispatch (by LaunchType, see agents/models.py) ---------

    def launch_app(self, launch_config: LaunchConfig) -> tuple[LocalStaticServer | None, BrowserWindow]:
        """Dispatch to the right launch routine for launch_config.launch_type."""
        if launch_config.launch_type == LaunchType.STATIC_WEB_SERVER:
            return self._launch_static_web_server(launch_config)
        if launch_config.launch_type == LaunchType.NATIVE_EXE:
            return self._launch_native_exe(launch_config)
        if launch_config.launch_type == LaunchType.ELECTRON_APP:
            return self._launch_electron_app(launch_config)
        if launch_config.launch_type == LaunchType.PYTHON_TKINTER:
            return self._launch_python_tkinter(launch_config)
        raise ValueError(f"Unknown launch_type: {launch_config.launch_type!r}")

    def _next_port(self, base_port: int) -> int:
        """A different port per launch (one per Phase, plus GUI-retries)
        means a different browser *origin* each time -- which means a
        naturally empty localStorage every launch, with zero explicit
        clearing needed. app.js never references its own port (pure
        localStorage, no fetch/WebSocket calls), so which port it's served
        on doesn't affect app behavior at all, only which origin the
        browser considers it to be.

        This replaces the old approach (typing a `javascript:
        localStorage.clear()` URL into the address bar), which needed
        toggling fullscreen off and back on to work reliably -- a visible,
        unwanted flicker. No clearing step at all means no flicker.
        """
        port = base_port + self._launch_count
        self._launch_count += 1
        return port

    def _launch_static_web_server(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer, BrowserWindow]:
        """Serve target-app/ and open it in the real default browser.

        We run our own controlled LocalStaticServer (readiness polling,
        clean subprocess teardown, UTF-8 env) rather than exec'ing
        launch_config.launch_command verbatim -- that string is LLM
        output, and shelling it out directly would be both fragile and a
        command-injection risk. The Developer's stated port is only the
        *base* -- see _next_port() for why each launch actually gets a
        different one.
        """
        base_port = urlparse(launch_config.entry_url).port or DEFAULT_PORT
        port = self._next_port(base_port)
        server = LocalStaticServer(self.config.target_app_dir, port=port)
        server.start()
        logger.info("GUI: launch_type=static_web_server serving at %s", server.url)

        entry_path = urlparse(launch_config.entry_url).path or "/index.html"
        url = f"http://127.0.0.1:{port}{entry_path}"
        # First launch of the whole run: open a genuinely new, separate
        # window (not Streamlit's). Every launch after that (next Phase,
        # or a GUI-retry within a Phase): add a new tab to that same
        # window instead of opening another one -- tabs piling up in the
        # one window is fine, cleanup() closes the whole window (and every
        # tab in it) at the very end.
        window = None
        if self._last_window is not None:
            if open_new_tab(self._last_window, url):
                window = self._last_window
            else:
                logger.info("GUI: previous window no longer exists, opening a new one")
        if window is None:
            window = open_browser_maximized(url)
        return server, window

    def _launch_native_exe(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer | None, BrowserWindow]:
        raise NotImplementedError("launch_type=native_exe 아직 미구현")

    def _launch_electron_app(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer | None, BrowserWindow]:
        """Launch the Electron app the Developer wrote into target-app/ and
        find/activate/maximize the resulting window.

        Deliberately ignores launch_config.launch_command/entry_url --
        same "don't exec LLM output for anything execution-critical" rule
        as _launch_static_web_server() not exec'ing the literal
        launch_command. The electron.exe binary lives at a fixed, known
        path because agents/developer.py's _ensure_electron_installed()
        put it there itself (never installed from the LLM's package.json
        content either).

        No LocalStaticServer here -- Electron serves its own renderer
        process directly, there's nothing to run our own http.server for.

        Two things confirmed necessary by direct testing (see
        agents/gui/execution.py's open_native_app_maximized() docstring
        for the window-handling side):
        - ELECTRON_RUN_AS_NODE must be stripped from the child's
          environment -- set globally in at least one real environment
          (the harness this was developed in), and it silently makes
          Electron run main.js as plain Node instead of actually launching
          the app (no window, no error, `app` just comes back undefined).
        - A fresh --user-data-dir per launch is Electron's equivalent of
          the web path's per-Phase port trick: a different profile
          directory means a naturally empty localStorage/IndexedDB every
          launch, no explicit clearing needed.
        """
        electron_bin = (
            self.config.target_app_dir / "node_modules" / "electron" / "dist" / "electron.exe"
        )
        if not electron_bin.exists():
            raise FileNotFoundError(
                f"{electron_bin} not found -- expected agents/developer.py's "
                "_ensure_electron_installed() to have put it there before GUI verification"
            )

        profile_dir = self.config.logs_dir / "electron_profiles" / f"profile_{self._launch_count}"
        self._launch_count += 1

        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        command = [str(electron_bin), ".", f"--user-data-dir={profile_dir}"]
        window = open_native_app_maximized(command, cwd=str(self.config.target_app_dir), env=env)
        return None, window

    def _launch_python_tkinter(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer | None, BrowserWindow]:
        """Launch the Tkinter app the Developer wrote into target-app/.

        Unlike the web path (per-Phase port) or Electron (per-Phase
        --user-data-dir), there's no framework/runtime-level flag that
        gives Tkinter a fresh state for free -- see the "Native EXE
        checklist" project note. Instead this relies on the generated
        app itself cooperating: agents/developer.py's TKINTER_SYSTEM_PROMPT
        requires main.py to support `python main.py --reset`, which must
        wipe any persisted state and exit immediately without ever
        creating a window. We run that as a blocking subprocess first,
        then launch the real (windowed) process the normal way, reusing
        the same generic open_native_app_maximized() used for Electron --
        no Tkinter-specific window-finding logic needed.
        """
        main_py = self.config.target_app_dir / "main.py"
        if not main_py.exists():
            raise FileNotFoundError(
                f"{main_py} not found -- expected agents/developer.py to have written it "
                "before GUI verification"
            )

        python_bin = sys.executable
        try:
            reset_result = subprocess.run(
                [python_bin, str(main_py), "--reset"],
                cwd=str(self.config.target_app_dir),
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
            )
            if reset_result.returncode != 0:
                logger.warning(
                    "GUI: launch_type=python_tkinter --reset exited non-zero (%d): %s",
                    reset_result.returncode,
                    reset_result.stderr.strip(),
                )
        except subprocess.TimeoutExpired:
            logger.warning(
                "GUI: launch_type=python_tkinter --reset timed out -- it may have "
                "incorrectly called mainloop() instead of exiting immediately"
            )

        command = [python_bin, str(main_py)]
        window = open_native_app_maximized(command, cwd=str(self.config.target_app_dir))
        return None, window

    # ---- step 4-3: full capture -> judge -> act -> verify loop ---------

    def verify_phase(self, phase: Phase, max_steps: int = MAX_GUI_STEPS) -> GUITestOutputSchema:
        """Drive the app for up to `max_steps` steps, trying to satisfy
        phase.success_criteria, and return the final GUITestOutputSchema.

        Does NOT touch plan.json -- that's the caller's (PlanDrivenPipeline)
        job.
        """
        if phase.launch_config is None:
            raise ValueError(
                f"Phase {phase.id} has no launch_config -- the Developer agent must "
                "produce one (DevOutputSchema.launch_config) before GUI verification can run"
            )

        self.config.screenshots_dir.mkdir(parents=True, exist_ok=True)

        step_entries: list[GUIStepLogEntry] = []
        step_history: list[str] = []
        screenshot_paths: list[str] = []
        consecutive_ineffective = 0
        note_for_model: str | None = None

        server, window = self.launch_app(phase.launch_config)
        try:
            # Fresh start each Phase -- launch_app() serves this launch on
            # its own port (see _next_port()), a different browser origin
            # each time, so there's no previous Phase's (or a human's
            # manual testing) leftover localStorage to worry about here.
            current_image = capture_screenshot(window)

            for step in range(1, max_steps + 1):
                boxes = detect_clickable_elements(current_image)
                labeled_image = overlay_labels(current_image, boxes)
                screenshot_paths.append(self._save_step_screenshot(phase, step, labeled_image))

                prompt_history = list(step_history)
                if note_for_model:
                    prompt_history.append(note_for_model)

                action = decide_next_action(
                    config=self.config,
                    labeled_screenshot=labeled_image,
                    success_criteria=phase.success_criteria,
                    step_log=prompt_history,
                )

                if action.action == "success":
                    step_entries.append(
                        GUIStepLogEntry(step=step, action="success 판단", result=action.reasoning)
                    )
                    logger.info("GUI: phase=%s success at step %d", phase.id, step)
                    return GUITestOutputSchema(
                        success=True, step_log=step_entries, screenshot_paths=screenshot_paths
                    )

                if action.action == "failure":
                    step_entries.append(
                        GUIStepLogEntry(step=step, action="failure 판단", result=action.reasoning)
                    )
                    logger.info("GUI: phase=%s judged failure at step %d", phase.id, step)
                    return GUITestOutputSchema(
                        success=False,
                        criterion_failed=action.criterion_failed,
                        step_log=step_entries,
                        symptom=action.reasoning,
                        screenshot_paths=screenshot_paths,
                    )

                action_desc = self._describe_action(action)
                box = next((b for b in boxes if b.index == action.target_element), None)

                if box is None:
                    result_text = f"{action.target_element}번 요소를 찾을 수 없어 실행하지 못함"
                    logger.warning("GUI: phase=%s step=%d %s", phase.id, step, result_text)
                    ineffective = True
                else:
                    click_at(window, *box.center)
                    if action.action == "type":
                        type_text(action.text or "")
                    time.sleep(ACTION_SETTLE_SECONDS)
                    new_image = capture_screenshot(window)

                    # A change can be small-and-local (typed text; a
                    # checkbox toggling + its label getting a strikethrough)
                    # or small-relative-to-the-screen-but-elsewhere (a new
                    # list item appearing below an "add" button that itself
                    # doesn't visually change). Neither check alone catches
                    # both, so treat it as changed if either does: the
                    # target box's own region (+ margin), OR the whole
                    # screen.
                    local_region = padded_region(box, LOCAL_DIFF_PADDING_PX, current_image.size)
                    changed = screenshots_differ(
                        current_image, new_image, region=local_region
                    ) or screenshots_differ(current_image, new_image)
                    current_image = new_image
                    result_text = "화면이 변경됨" if changed else "화면이 변경되지 않음"
                    ineffective = not changed

                step_entries.append(GUIStepLogEntry(step=step, action=action_desc, result=result_text))
                step_history.append(f"{action_desc} -> {result_text}")
                logger.info("GUI: phase=%s step=%d %s -> %s", phase.id, step, action_desc, result_text)

                if ineffective:
                    consecutive_ineffective += 1
                    if consecutive_ineffective >= CONSECUTIVE_INEFFECTIVE_LIMIT:
                        note_for_model = (
                            f"최근 {consecutive_ineffective}회 연속으로 액션이 화면 변화를 "
                            f"일으키지 못했습니다 (마지막 시도: {action_desc}). 같은 요소를 다시 "
                            "시도하지 말고 다른 요소나 접근을 고려하세요."
                        )
                    else:
                        note_for_model = None
                else:
                    consecutive_ineffective = 0
                    note_for_model = None

            symptom = self._summarize_step_limit(step_entries)
            logger.warning("GUI: phase=%s exceeded max_steps=%d -- %s", phase.id, max_steps, symptom)
            return GUITestOutputSchema(
                success=False, step_log=step_entries, symptom=symptom, screenshot_paths=screenshot_paths
            )
        finally:
            if server is not None:
                server.stop()
            self._last_window = window

    # ---- internals -------------------------------------------------------

    def _describe_action(self, action: GUIActionSchema) -> str:
        if action.action == "click":
            return f"{action.target_element}번 요소 클릭"
        if action.action == "type":
            return f"{action.target_element}번 요소에 '{action.text}' 입력"
        return action.action

    def _save_step_screenshot(self, phase: Phase, step: int, labeled_image: Image.Image) -> str:
        """Save the step screenshot and return its path relative to
        logs_dir (e.g. 'screenshots/xxx.png') -- what the development
        report (agents/report.py) embeds as a markdown image link, since
        the report file itself lives directly under logs/."""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = self.config.screenshots_dir / f"{timestamp}_{phase.id}_step{step}_labeled.png"
        labeled_image.save(path)
        return path.relative_to(self.config.logs_dir).as_posix()

    def _summarize_step_limit(self, step_entries: list[GUIStepLogEntry]) -> str:
        actions_summary = "; ".join(f"{e.action} -> {e.result}" for e in step_entries)
        return (
            f"최대 스텝({len(step_entries)}회)을 시도했지만 success_criteria 충족 여부를 "
            f"확인하지 못했습니다. 수행 내역: {actions_summary}"
        )
