"""GUI Tester agent implementation.

Scope so far:
- 4-1: serve target-app/ locally, open it in the system's default browser,
  capture a real on-screen screenshot (PyAutoGUI), detect clickable-looking
  elements via OpenCV, and overlay numbered Set-of-marks labels.
- 4-2: send the labeled screenshot (image only -- no separate OCR text
  list) plus the Phase's success_criteria to the Vision model and get back
  a single next-action decision (agents/gui/judgment.py).
- 4-3: execute that action for real via PyAutoGUI (agents/gui/execution.py)
  and loop capture -> judge -> execute up to MAX_GUI_STEPS times, building
  a step_log and returning a GUITestOutputSchema. Exposed as verify_phase().
  Deliberately no code-side "did the screen change" verdict is fed back
  into the model's history -- success/failure is judged purely from what
  the model itself sees in each new labeled screenshot, the same way a
  human tester would (a prior pixel-diff heuristic here produced a
  confirmed false negative that overrode the model's own correct reading).

Local OCR (agents/gui/ocr.py) is intentionally NOT used here: this is a
GPU-less environment, so per-box OCR was dropped in favor of letting the
Vision model read the screenshot image directly. The module is kept
un-deleted in case local OCR is worth reintroducing later.

verify() (the ABC method the orchestrator calls) is not implemented yet --
that's step 4-4, which will also wire verify_phase()'s result into
plan.json and the Developer feedback loop.
"""

from __future__ import annotations

import json
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
)
from agents.gui.execution import (
    BrowserWindow,
    WindowLostError,
    capture_screenshot,
    click_at,
    close_window,
    drag_at,
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

MAX_GUI_STEPS = 60
ACTION_SETTLE_SECONDS = 0.5


class OpenAIGUITesterAgent(GUITesterAgent):
    """Named for historical reasons (this whole pipeline's other three
    agents are OpenAI-only) -- its own vision judgment call is actually
    multi-provider, see agents/gui/judgment.py and config.gui_tester_provider."""

    def __init__(self, config: PipelineConfig | None = None) -> None:
        self.config = config or PipelineConfig()
        self._last_window: BrowserWindow | None = None
        self._launch_count = 0
        # Fresh-state-per-launch used to mean "per verify_phase() call"
        # (every Phase, every GUI-retry). Now it means "once per this GUI
        # Tester instance's whole run" -- see each _launch_xxx() method for
        # why. None/False until the first launch claims them.
        self._session_port: int | None = None
        self._session_profile_dir: Path | None = None
        self._session_reset_done = False
        # Persists across every Phase and every GUI-retry of the whole
        # run, same lifetime as the state above -- without this, the app's
        # *data* carries forward (accounts, kanban cards, ...) but the
        # model's own memory of what it already did does not, since
        # step_history used to be a fresh empty list every verify_phase()
        # call. Confirmed causing real failures: the model would try to
        # sign up an already-registered username again (gets stuck on
        # "already exists"), or try to log into a username it never
        # actually registered (gets stuck on "invalid credentials") --
        # both because it had no memory of which accounts it had already
        # created in an earlier Phase.
        self._step_history: list[str] = []
        self._load_persisted_session()
        enable_windows_dpi_awareness()

    def _load_persisted_session(self) -> None:
        """Restore session-level launch state (port / Electron profile dir
        / whether Tkinter's --reset already ran) from disk, if present.

        Without this, a *resumed* run (new Python process, but an
        existing plan.json with earlier Phases already gui_verified)
        looks identical -- from this fresh instance's point of view -- to
        a genuinely new run, so it would re-assign a new port/profile-dir
        or re-run --reset, wiping exactly the state a resume is supposed
        to preserve. Confirmed real: a Tkinter run's state.json had
        `current_user` reset to null between Phases despite `users`/
        `boards` staying intact. Best-effort: any failure here just means
        falling back to fresh-session behavior, never raises.
        """
        try:
            if not self.config.gui_session_file.exists():
                return
            data = json.loads(self.config.gui_session_file.read_text(encoding="utf-8"))
            self._session_port = data.get("port")
            profile_dir = data.get("profile_dir")
            self._session_profile_dir = Path(profile_dir) if profile_dir else None
            self._session_reset_done = bool(data.get("reset_done", False))
            logger.info(
                "GUI: resumed session state from %s (port=%s, profile_dir=%s, reset_done=%s)",
                self.config.gui_session_file,
                self._session_port,
                self._session_profile_dir,
                self._session_reset_done,
            )
        except Exception as exc:
            logger.warning(
                "GUI: could not load persisted session state, treating this as a fresh "
                "session (%s)",
                exc,
            )

    def _persist_session(self) -> None:
        """Best-effort: write the current session launch state to disk so
        a later resumed process can restore it via _load_persisted_session().
        Never raises -- a failure here just means a later resume won't be
        clean, not that this run breaks."""
        try:
            self.config.gui_session_file.parent.mkdir(parents=True, exist_ok=True)
            self.config.gui_session_file.write_text(
                json.dumps(
                    {
                        "port": self._session_port,
                        "profile_dir": str(self._session_profile_dir)
                        if self._session_profile_dir
                        else None,
                        "reset_done": self._session_reset_done,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            logger.warning("GUI: could not persist session state (%s)", exc)

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
            phase_id=phase.id,
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
        """Picks a port different from a previous run's leftover browser
        state, giving a fresh origin (= naturally empty localStorage) once
        per this GUI Tester instance's run -- called exactly once, by
        _launch_static_web_server(), the first time it's invoked (see
        self._session_port). app.js never references its own port (pure
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

    def _close_previous_native_window(self) -> None:
        """Close the window from the previous native (Electron/Tkinter)
        launch, if any, before starting a new one.

        Unlike the web path's open_new_tab() (which reuses one OS window
        across launches for real -- same process, just a new tab/page
        load), a native app's window *is* its process: a running Electron
        or Tkinter process has already loaded whatever code was on disk
        when it started, so simply leaving it open and "reusing" it would
        silently keep testing stale code after a Developer rewrite. This
        doesn't try to reuse the window -- it still launches a genuinely
        new process every time, so the newest code always runs -- it just
        stops leaving the *previous* one orphaned. Before this, every
        Phase/retry left its own window open indefinitely (only the very
        last one got closed by cleanup() at the end of the whole run),
        piling up windows exactly like the web path used to before
        open_new_tab() was added. Best-effort, matching cleanup()'s own
        style: never raises, just logs and moves on.
        """
        if self._last_window is None:
            return
        try:
            close_window(self._last_window)
        except Exception as exc:
            logger.warning("GUI: could not close previous native window (%s)", exc)
        finally:
            self._last_window = None

    def _launch_static_web_server(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer, BrowserWindow]:
        """Serve target-app/ and open it in the real default browser.

        We run our own controlled LocalStaticServer (readiness polling,
        clean subprocess teardown, UTF-8 env) rather than exec'ing
        launch_config.launch_command verbatim -- that string is LLM
        output, and shelling it out directly would be both fragile and a
        command-injection risk. The Developer's stated port is only the
        *base* -- see _next_port() for why the run's first launch actually
        gets a different one.
        """
        base_port = urlparse(launch_config.entry_url).port or DEFAULT_PORT
        if self._session_port is None:
            # Fresh origin only once per run now, not once per launch --
            # see the __init__ comment for why (confirmed: later Phases
            # depend on earlier Phases' state, e.g. an account created in
            # Phase 2, so wiping it before every Phase forced the model to
            # redo signup/login inside every single Phase's step budget).
            self._session_port = self._next_port(base_port)
            self._persist_session()
        port = self._session_port
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
        - A fresh --user-data-dir once per run is Electron's equivalent of
          the web path's per-run port trick (see _launch_static_web_server()
          and the __init__ comment): a different profile directory means a
          naturally empty localStorage/IndexedDB for the run's first
          launch, no explicit clearing needed. Only assigned once per run
          now, not once per launch -- later Phases reuse the same profile
          so they can build on state earlier Phases already established
          (e.g. a logged-in session), instead of every Phase forcing the
          model to redo setup from scratch inside its own step budget.
        """
        electron_bin = (
            self.config.target_app_dir / "node_modules" / "electron" / "dist" / "electron.exe"
        )
        if not electron_bin.exists():
            raise FileNotFoundError(
                f"{electron_bin} not found -- expected agents/developer.py's "
                "_ensure_electron_installed() to have put it there before GUI verification"
            )

        if self._session_profile_dir is None:
            self._session_profile_dir = (
                self.config.logs_dir / "electron_profiles" / f"profile_{self._launch_count}"
            )
            self._launch_count += 1
            self._persist_session()
        profile_dir = self._session_profile_dir

        self._close_previous_native_window()
        env = {k: v for k, v in os.environ.items() if k != "ELECTRON_RUN_AS_NODE"}
        command = [str(electron_bin), ".", f"--user-data-dir={profile_dir}"]
        window = open_native_app_maximized(command, cwd=str(self.config.target_app_dir), env=env)
        return None, window

    def _launch_python_tkinter(
        self, launch_config: LaunchConfig
    ) -> tuple[LocalStaticServer | None, BrowserWindow]:
        """Launch the Tkinter app the Developer wrote into target-app/.

        Unlike the web path (per-run port) or Electron (per-run
        --user-data-dir), there's no framework/runtime-level flag that
        gives Tkinter a fresh state for free -- see the "Native EXE
        checklist" project note. Instead this relies on the generated
        app itself cooperating: agents/developer.py's TKINTER_SYSTEM_PROMPT
        requires main.py to support `python main.py --reset`, which must
        wipe any persisted state and exit immediately without ever
        creating a window. We run that as a blocking subprocess first --
        but only before the *first* launch of a run (see __init__), not
        before every Phase/retry -- then launch the real (windowed)
        process the normal way, reusing the same generic
        open_native_app_maximized() used for Electron -- no
        Tkinter-specific window-finding logic needed.
        """
        main_py = self.config.target_app_dir / "main.py"
        if not main_py.exists():
            raise FileNotFoundError(
                f"{main_py} not found -- expected agents/developer.py to have written it "
                "before GUI verification"
            )

        python_bin = sys.executable
        if not self._session_reset_done:
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
            self._session_reset_done = True
            self._persist_session()

        self._close_previous_native_window()
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
        screenshot_paths: list[str] = []
        # Tracks literal repetition of the exact same (action, target)
        # choice back-to-back -- reset per verify_phase() call (a new
        # Phase/retry attempt is a legitimately fresh context). Unlike the
        # removed screenshots_differ() heuristic, this makes no claim
        # about the screen at all -- it's just counting the model's own
        # choices, so it can't be factually wrong the way that was.
        last_action_signature: tuple | None = None
        consecutive_same_action = 0

        # Drop this Phase's own step_history entries from any *previous*
        # GUI-retry attempt before starting this one -- confirmed causing
        # a real bug: a GUI failure triggers a Developer rewrite (the
        # underlying code actually changes), but self._step_history is an
        # instance attribute that otherwise survives across attempts, only
        # tagged by phase.id (bug 16's fix distinguishes *other* Phases,
        # not *other attempts of this same Phase*). The next attempt's
        # judgment call was seeing the previous attempt's action (e.g. "이미
        # 드래그했지만 이동하지 않음", from code that no longer exists) as
        # if it were current-attempt evidence, and immediately judged
        # failure again at step 1 without ever driving the rewritten app at
        # all -- three retries in a row failing "step 1, no action taken"
        # with identical reasoning was the tell. Other Phases' entries are
        # untouched here since their code wasn't rewritten and is still
        # valid background (accounts/data that exist, current login state).
        self._step_history = [
            entry for entry in self._step_history if not entry.startswith(f"[{phase.id}] ")
        ]

        server, window = self.launch_app(phase.launch_config)
        try:
            # State (accounts, saved data, ...) now carries forward from
            # earlier Phases within this same run -- only the very first
            # launch of the whole run gets a clean slate (see
            # self._session_port/_session_profile_dir/_session_reset_done
            # above). self._step_history (below) carries forward too, so
            # the model has its own memory of what it already set up.
            current_image = capture_screenshot(window)

            for step in range(1, max_steps + 1):
                boxes = detect_clickable_elements(current_image)
                labeled_image = overlay_labels(current_image, boxes)
                screenshot_paths.append(self._save_step_screenshot(phase, step, labeled_image))

                action = decide_next_action(
                    config=self.config,
                    labeled_screenshot=labeled_image,
                    success_criteria=phase.success_criteria,
                    phase_id=phase.id,
                    current_step=step,
                    max_steps=max_steps,
                    step_log=self._step_history,
                )

                if action.action == "success":
                    step_entries.append(
                        GUIStepLogEntry(step=step, action="success 판단", result=action.reasoning)
                    )
                    # So a later Phase knows this one already passed (and
                    # why) -- helps avoid re-testing something already
                    # confirmed, same reasoning as tagging every step above.
                    self._step_history.append(f"[{phase.id}] 완료: {action.reasoning}")
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

                action_signature = (action.action, action.target_element, action.drop_target_element)
                if action_signature == last_action_signature:
                    consecutive_same_action += 1
                else:
                    consecutive_same_action = 1
                last_action_signature = action_signature

                if action.action == "drag":
                    drop_box = next(
                        (b for b in boxes if b.index == action.drop_target_element), None
                    )
                    missing = []
                    if box is None:
                        missing.append(f"{action.target_element}번(대상)")
                    if drop_box is None:
                        missing.append(f"{action.drop_target_element}번(목적지)")
                    if missing:
                        result_text = f"{', '.join(missing)} 요소를 찾을 수 없어 실행하지 못함"
                        logger.warning("GUI: phase=%s step=%d %s", phase.id, step, result_text)
                    else:
                        drag_at(window, box.center, drop_box.center)
                        time.sleep(ACTION_SETTLE_SECONDS)
                        current_image = capture_screenshot(window)
                        result_text = "실행함"
                elif box is None:
                    result_text = f"{action.target_element}번 요소를 찾을 수 없어 실행하지 못함"
                    logger.warning("GUI: phase=%s step=%d %s", phase.id, step, result_text)
                else:
                    click_at(window, *box.center)
                    if action.action == "type":
                        type_text(action.text or "")
                    time.sleep(ACTION_SETTLE_SECONDS)
                    # No pixel-diff verdict here on purpose: a code-side
                    # "screen changed / didn't change" claim can be wrong
                    # (confirmed by a real false negative -- a Tkinter
                    # counter's digit swap moved fewer pixels than the old
                    # global-ratio threshold required), and once stated as
                    # fact in step_history the model trusted it over its
                    # own correct reading of the next screenshot. Just
                    # record that the action was executed; the model judges
                    # success/failure purely from what it actually sees in
                    # the next labeled screenshot, the same way a human
                    # tester would.
                    current_image = capture_screenshot(window)
                    result_text = "실행함"

                step_entries.append(GUIStepLogEntry(step=step, action=action_desc, result=result_text))
                # Tagged with phase.id since this list now spans every
                # Phase/retry of the whole run, not just this call -- lets
                # the model tell "this happened just now" apart from "this
                # was a different, already-completed Phase."
                self._step_history.append(f"[{phase.id}] {action_desc} -> {result_text}")
                logger.info("GUI: phase=%s step=%d %s -> %s", phase.id, step, action_desc, result_text)

                if consecutive_same_action >= 3:
                    self._step_history.append(
                        f"[{phase.id}] 시스템 안내: 같은 액션({action_desc})을 연속 "
                        f"{consecutive_same_action}번 선택했습니다. 화면을 왔다갔다만 하는 것은 "
                        "새로운 정보를 주지 않습니다. 완전히 다른 요소를 시도하거나, 지금까지 "
                        "확인한 내용만으로 지금 바로 성공/실패를 판단하세요."
                    )
                    logger.warning(
                        "GUI: phase=%s step=%d same action repeated %d times in a row",
                        phase.id,
                        step,
                        consecutive_same_action,
                    )

            symptom = self._summarize_step_limit(step_entries)
            logger.warning("GUI: phase=%s exceeded max_steps=%d -- %s", phase.id, max_steps, symptom)
            return GUITestOutputSchema(
                success=False, step_log=step_entries, symptom=symptom, screenshot_paths=screenshot_paths
            )
        except WindowLostError as exc:
            # The target app's window disappeared mid-Phase (most likely
            # crashed after a click) -- without this, capture_screenshot()
            # would otherwise have silently fallen back to whatever now
            # occupies the same screen coordinates (e.g. the dashboard
            # browser behind it), and the model would burn its whole step
            # budget confusedly clicking around that instead, with no way
            # to know the real problem. Fail the Phase immediately with a
            # clear diagnostic instead -- this routes into the same
            # Developer-rewrite retry path as any other GUI failure, so a
            # crash-causing bug gets a real chance to be fixed.
            symptom = f"앱 창이 검증 도중 사라졌습니다(충돌했을 가능성이 높음): {exc}"
            logger.warning("GUI: phase=%s window lost mid-verification -- %s", phase.id, symptom)
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
        if action.action == "drag":
            return f"{action.target_element}번 요소를 {action.drop_target_element}번 위치로 드래그"
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
