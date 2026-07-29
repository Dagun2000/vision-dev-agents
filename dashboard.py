"""Chat-style Streamlit dashboard for the multi-agent pipeline.

Run with:
    streamlit run dashboard.py

Flow: type a requirement in the chat box -> a background thread runs
Planner.create_plan() and then PlanDrivenPipeline.run_all_phases() -> plan.json
is polled every ~1.5s and phase status changes are appended to the chat as
assistant messages -> if the Planner needs a human (request_human_escalation,
which blocks on the builtin input()), the prompt is shown in chat and the
next message typed in the chat box is fed back into that blocked call -> on
completion, the final report is offered as a chat message + download button.

GUI Tester drives the *real* screen (PyAutoGUI) once a Phase reaches GUI
verification -- don't touch the mouse/keyboard while a run is in progress.
"""

from __future__ import annotations

import builtins
import dataclasses
import json
import queue
import threading
import time
from pathlib import Path

import streamlit as st

from agents.developer import OpenAIDeveloperAgent
from agents.gui_tester import OpenAIGUITesterAgent
from agents.models import LaunchType
from agents.planner import OpenAIPlannerAgent
from agents.reviewer import OpenAIReviewerAgent
from orchestrator.config import PipelineConfig
from orchestrator.logging_setup import setup_logging
from orchestrator.plan_pipeline import PlanDrivenPipeline

st.set_page_config(page_title="Vision Dev Agents", page_icon="🤖")

POLL_INTERVAL_SECONDS = 1.5

# STATIC_WEB_SERVER and ELECTRON_APP are both implemented now
# (agents/gui_tester.py's launch_app() dispatcher). NATIVE_EXE still isn't
# -- the selector still lists it so the target platform is a visible,
# explicit choice rather than an assumption baked into the code.
LAUNCH_TYPE_LABELS: dict[LaunchType, str] = {
    LaunchType.STATIC_WEB_SERVER: "웹 앱 (정적 웹, HTML/CSS/JS)",
    LaunchType.ELECTRON_APP: "Electron 앱 (데스크톱)",
    LaunchType.NATIVE_EXE: "데스크톱 앱 (Native EXE) -- 아직 미지원",
}
UNSUPPORTED_LAUNCH_TYPES = {LaunchType.NATIVE_EXE}

STATUS_LABELS = {
    "dev_done": "개발 완료, 코드 리뷰 진행 중...",
    "lint_failed": "Lint 실패",
    "review_done": "코드 리뷰 통과, GUI 검증 진행 중...",
    "review_failed": "코드 리뷰 실패",
    "gui_verified": "완료 ✓",
    "gui_test_failed": "GUI 검증 실패 (재시도 소진)",
    "skipped": "사람이 스킵을 선택하여 건너뜀",
}


class PipelineBridge:
    """Connects one background pipeline run to the chat UI.

    request_human_escalation() (agents/planner.py) blocks on the builtin
    input(). While this bridge's thread is running, it replaces
    builtins.input with bridged_input(), which posts the prompt to the chat
    via `events` and then blocks on `input_queue` -- unblocked only when the
    Streamlit script (running in the main thread) puts the user's next chat
    message there.
    """

    def __init__(self) -> None:
        self.events: queue.Queue[dict] = queue.Queue()
        self.input_queue: queue.Queue[str] = queue.Queue()
        self.awaiting_input = threading.Event()
        self.finished = threading.Event()

    def bridged_input(self, prompt: str = "") -> str:
        self.events.put({"content": f"🔔 {prompt}".strip()})
        self.awaiting_input.set()
        answer = self.input_queue.get()
        self.awaiting_input.clear()
        return answer

    def start(self, requirement: str, config: PipelineConfig) -> None:
        thread = threading.Thread(target=self._run, args=(requirement, config), daemon=True)
        thread.start()

    def _run(self, requirement: str, config: PipelineConfig) -> None:
        original_input = builtins.input
        builtins.input = self.bridged_input
        try:
            setup_logging(config.logs_dir)
            planner = OpenAIPlannerAgent(config=config)

            if config.plan_file.exists():
                self.events.put({"content": "기존 계획(state/plan.json)을 이어서 진행합니다."})
            else:
                phases = planner.create_plan(requirement)
                lines = "\n".join(f"{i + 1}. {p.title}" for i, p in enumerate(phases))
                self.events.put(
                    {"content": f"다음과 같이 {len(phases)}단계로 나눠서 진행하겠습니다:\n{lines}"}
                )

            pipeline = PlanDrivenPipeline(
                planner=planner,
                developer=OpenAIDeveloperAgent(config=config),
                reviewer=OpenAIReviewerAgent(config=config),
                gui_tester=OpenAIGUITesterAgent(config=config),
                config=config,
            )
            pipeline.run_all_phases(requirement)

            report_path = _latest_report_path(config)
            event = {"content": "모든 Phase가 완료됐습니다."}
            if report_path is not None:
                event["report_path"] = str(report_path)
            self.events.put(event)
        except Exception as exc:  # noqa: BLE001 -- surface any failure in the chat instead of dying silently
            self.events.put({"content": f"⚠️ 파이프라인 실행 중 오류가 발생했습니다: {exc}"})
        finally:
            builtins.input = original_input
            self.finished.set()


def _latest_report_path(config: PipelineConfig) -> Path | None:
    candidates = sorted(config.logs_dir.glob("report_*.md"))
    return candidates[-1] if candidates else None


def _phase_snapshot(phase_dict: dict) -> dict:
    return {
        "status": phase_dict["status"],
        "gui_retry_count": phase_dict.get("gui_retry_count", 0),
        "human_escalation_choice": phase_dict.get("human_escalation_choice"),
    }


def _init_state() -> None:
    if "messages" not in st.session_state:
        st.session_state.messages = []
    if "bridge" not in st.session_state:
        st.session_state.bridge = None
    if "phase_snapshot" not in st.session_state:
        st.session_state.phase_snapshot = {}
    if "snapshot_seeded" not in st.session_state:
        st.session_state.snapshot_seeded = False
    if "config" not in st.session_state:
        st.session_state.config = PipelineConfig()


def _drain_bridge_events(bridge: PipelineBridge) -> None:
    while True:
        try:
            event = bridge.events.get_nowait()
        except queue.Empty:
            break
        st.session_state.messages.append({"role": "assistant", **event})


def _poll_plan_changes(config: PipelineConfig) -> None:
    if not config.plan_file.exists():
        return
    try:
        plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return  # plan.json is mid-write -- try again next tick

    phases = plan.get("phases", [])
    if not phases:
        return

    if not st.session_state.snapshot_seeded:
        # First sight of the plan -- record it silently so the phase-list
        # message from the bridge (not this diff) is what introduces it.
        st.session_state.phase_snapshot = {p["id"]: _phase_snapshot(p) for p in phases}
        st.session_state.snapshot_seeded = True
        return

    total = len(phases)
    for idx, phase_dict in enumerate(phases):
        phase_id = phase_dict["id"]
        snapshot = _phase_snapshot(phase_dict)
        previous = st.session_state.phase_snapshot.get(phase_id)
        label = f"[{phase_dict['title']} ({idx + 1}/{total})]"

        if previous is None:
            replanned_from = phase_dict.get("replanned_from")
            if replanned_from:
                st.session_state.messages.append(
                    {
                        "role": "assistant",
                        "content": f"[재계획] '{replanned_from}' Phase 접근 방식을 조정하여 새 Phase '{phase_id}' 생성됨",
                    }
                )
            st.session_state.messages.append(
                {"role": "assistant", "content": f"{label} {STATUS_LABELS.get(snapshot['status'], snapshot['status'])}"}
            )
        elif previous["status"] != snapshot["status"]:
            st.session_state.messages.append(
                {"role": "assistant", "content": f"{label} {STATUS_LABELS.get(snapshot['status'], snapshot['status'])}"}
            )
        elif previous["gui_retry_count"] != snapshot["gui_retry_count"] and snapshot["gui_retry_count"]:
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": (
                        f"{label} GUI 검증 실패, 재시도 중 "
                        f"({snapshot['gui_retry_count']}/{config.max_gui_test_retries})"
                    ),
                }
            )
        elif (
            previous["human_escalation_choice"] != snapshot["human_escalation_choice"]
            and snapshot["human_escalation_choice"]
        ):
            st.session_state.messages.append(
                {"role": "assistant", "content": f"{label} 사람 선택: {snapshot['human_escalation_choice']}"}
            )

        st.session_state.phase_snapshot[phase_id] = snapshot


def main() -> None:
    _init_state()
    config: PipelineConfig = st.session_state.config
    bridge: PipelineBridge | None = st.session_state.bridge

    pipeline_active = bridge is not None and not bridge.finished.is_set()
    if bridge is not None:
        # Poll plan.json before draining the bridge's own events, so a
        # phase's status-change message lands in the transcript before the
        # "all done" summary that the background thread posts right after.
        # Poll even on the render where we first see bridge.finished -- the
        # background thread can flip that flag between one render and the
        # next, which would otherwise silently drop the last phase's status
        # change.
        if not bridge.awaiting_input.is_set():
            _poll_plan_changes(config)
        _drain_bridge_events(bridge)

    st.title("🤖 Vision Dev Agents")
    st.caption("요구사항을 입력하면 Planner → Developer → Reviewer → GUI Tester 파이프라인이 자동으로 실행됩니다.")

    selected_launch_type = st.selectbox(
        "앱 타입",
        options=list(LAUNCH_TYPE_LABELS.keys()),
        format_func=lambda lt: LAUNCH_TYPE_LABELS[lt],
        key="launch_type_select",
        disabled=pipeline_active,
    )

    for i, msg in enumerate(st.session_state.messages):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            report_path_str = msg.get("report_path")
            if report_path_str:
                report_path = Path(report_path_str)
                if report_path.exists():
                    st.download_button(
                        "리포트 다운로드",
                        data=report_path.read_bytes(),
                        file_name=report_path.name,
                        mime="text/markdown",
                        key=f"report_dl_{i}",
                    )

    awaiting_input = bridge is not None and bridge.awaiting_input.is_set()
    placeholder = "답변을 입력하세요 (예: 1)" if awaiting_input else "구현하고 싶은 앱 요구사항을 입력하세요"
    prompt = st.chat_input(placeholder)

    if prompt:
        if awaiting_input:
            st.session_state.messages.append({"role": "user", "content": prompt})
            bridge.input_queue.put(prompt)
        elif pipeline_active:
            st.session_state.messages.append(
                {"role": "assistant", "content": "이미 파이프라인이 실행 중입니다. 완료될 때까지 기다려주세요."}
            )
        elif selected_launch_type in UNSUPPORTED_LAUNCH_TYPES:
            st.session_state.messages.append(
                {
                    "role": "assistant",
                    "content": (
                        f"'{LAUNCH_TYPE_LABELS[selected_launch_type]}'는 아직 지원되지 않습니다. "
                        f"앱 타입을 '{LAUNCH_TYPE_LABELS[LaunchType.STATIC_WEB_SERVER]}'로 선택한 뒤 다시 시도해주세요."
                    ),
                }
            )
        else:
            st.session_state.messages.append({"role": "user", "content": prompt})
            new_bridge = PipelineBridge()
            st.session_state.bridge = new_bridge
            st.session_state.phase_snapshot = {}
            st.session_state.snapshot_seeded = False
            # target_launch_type is a per-run choice (the dashboard's own
            # selectbox), not an .env deployment setting -- override it on
            # a copy of the base config rather than mutating the shared one.
            run_config = dataclasses.replace(config, target_launch_type=selected_launch_type)
            new_bridge.start(prompt, run_config)
        st.rerun()

    if pipeline_active:
        time.sleep(POLL_INTERVAL_SECONDS)
        st.rerun()


main()
