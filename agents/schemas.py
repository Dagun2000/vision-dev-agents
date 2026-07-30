"""Pydantic schemas used for LLM structured-output parsing.

Kept separate from agents/models.py: these describe the exact JSON shape
requested from the model, while models.py holds the plain dataclasses used
internally across agents and the orchestrator.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from agents.models import LaunchType


class PlanPhaseSchema(BaseModel):
    id: str = Field(description="Sequential id, e.g. 'phase-1', 'phase-2'")
    title: str
    description: str
    success_criteria: list[str] = Field(
        description="One or more concrete, screen-observable conditions used by the "
        "Reviewer/GUI Tester agents to judge pass/fail"
    )
    status: Literal["pending"] = "pending"


class PlanSchema(BaseModel):
    phases: list[PlanPhaseSchema]


class LaunchConfigSchema(BaseModel):
    """How to launch target-app/ for GUI verification.

    STATIC_WEB_SERVER and ELECTRON_APP are both implemented (see
    agents/gui_tester.py's launch_app()) -- the Developer agent's prompt
    fixes launch_type to whichever one this run actually targets
    (PipelineConfig.target_launch_type), not something the model chooses
    freely. Note: for both types, the GUI Tester constructs its own actual
    launch command rather than exec'ing launch_command verbatim (LLM
    output, unsafe/fragile to shell out directly) -- these two fields are
    mostly documentation of intent at this point, kept for the report and
    for a future launch_type where the literal command might matter more.
    """

    launch_type: LaunchType = Field(
        description="이번 실행에서 지정된 값 그대로 반환하세요 (직접 고르지 마세요)"
    )
    launch_command: str = Field(description="앱 실행 커맨드, 예: 'python -m http.server 8000'")
    entry_url: str = Field(description="접속 URL, 예: 'http://localhost:8000/index.html' (Electron이면 빈 문자열)")


class DevOutputSchema(BaseModel):
    """Full contents of the app files for one Developer attempt.

    index_html/style_css/app_js are always required -- they're the UI for
    both launch types (a static web app serves them directly; an Electron
    app's BrowserWindow loads the same index.html as its renderer).
    main_js/package_json are additionally required only when
    launch_config.launch_type is electron_app (Electron's main-process
    entry point + manifest); left null for static_web_server.

    The model always returns full file contents (not a diff) so each Phase
    can accumulate on top of the previous Phase's output.
    """

    index_html: str = Field(description="index.html 전체 내용")
    style_css: str = Field(description="style.css 전체 내용")
    app_js: str = Field(description="app.js 전체 내용 (바닐라 JS, module 금지)")
    launch_config: LaunchConfigSchema = Field(description="이 앱을 어떻게 실행해서 검증할지")
    summary: str = Field(description="이번 Phase에서 구현/수정한 내용에 대한 한두 문장 요약")
    main_js: str | None = Field(
        default=None,
        description="Electron main.js 전체 내용. launch_type이 electron_app일 때만 채우고, "
        "static_web_server면 null로 두세요.",
    )
    package_json: str | None = Field(
        default=None,
        description="package.json 전체 내용 (main 필드는 반드시 'main.js'). launch_type이 "
        "electron_app일 때만 채우고, static_web_server면 null로 두세요.",
    )
    tkinter_main_py: str | None = Field(
        default=None,
        description="Tkinter main.py 전체 내용. launch_type이 python_tkinter일 때만 채우고, "
        "그 외에는 null로 두세요.",
    )


class ReviewOutputSchema(BaseModel):
    """Reviewer's semantic judgement of whether code satisfies success_criteria."""

    approved: bool = Field(description="모든 success_criteria를 의미적으로 만족하면 true")
    issues: list[str] = Field(
        default_factory=list,
        description="approved가 false일 때, 어떤 success_criteria가 왜 충족되지 않는지 "
        "구체적으로 적은 목록. approved가 true면 빈 배열",
    )


class GUIActionSchema(BaseModel):
    """One next-step decision from the Vision judgment call (agents/gui/judgment.py).

    The model is shown only the labeled (Set-of-marks) screenshot -- no
    separate OCR text list -- and must read the image itself to decide
    what element N is and what to do next.
    """

    action: Literal["click", "type", "drag", "success", "failure"] = Field(
        description="click: N번 요소를 클릭한다. type: N번 요소에 텍스트를 입력한다. "
        "drag: target_element 요소를 drop_target_element 요소 위로 드래그해서 놓는다. "
        "success: 화면 상태만으로 success_criteria가 이미 충족되었다고 판단됨. "
        "failure: 더 진행해도 success_criteria를 충족시킬 수 없다고 판단됨."
    )
    target_element: int | None = Field(
        default=None,
        description="action이 click/type/drag일 때, 대상(드래그할) 요소의 번호 (스크린샷의 빨간 배지 숫자)",
    )
    drop_target_element: int | None = Field(
        default=None,
        description="action이 drag일 때, target_element를 놓을 목적지 요소의 번호. "
        "drag가 아니면 null.",
    )
    text: str | None = Field(default=None, description="action이 type일 때 입력할 텍스트")
    criterion_failed: str | None = Field(
        default=None,
        description="action이 failure일 때, success_criteria 중 충족되지 않은 항목 (원문 그대로 또는 "
        "가장 가까운 항목). action이 failure가 아니면 null",
    )
    reasoning: str = Field(
        description="이 판단을 내린 이유를 화면에서 관찰한 내용을 근거로 한두 문장으로 서술"
    )


class GUIStepLogEntry(BaseModel):
    """One step of the GUI Tester's action/observation trace."""

    step: int
    action: str = Field(description="이번 스텝에서 수행한 액션 (예: '3번 요소 클릭', '1번 입력창에 텍스트 입력')")
    result: str = Field(description="액션 수행 후 관찰된 결과에 대한 짧은 서술")


class GUITestOutputSchema(BaseModel):
    """Final result of one GUI Tester verification run for a Phase."""

    success: bool
    criterion_failed: str | None = Field(
        default=None,
        description="success가 false일 때, success_criteria 중 충족되지 않은 항목. "
        "success가 true면 null",
    )
    step_log: list[GUIStepLogEntry] = Field(default_factory=list)
    symptom: str | None = Field(
        default=None,
        description="success가 false일 때, 화면에서 실제로 관찰된 증상을 "
        "'무엇을 기대했는데 실제로는 어땠는지' 형태의 자연어로 서술. success가 true면 null",
    )
    screenshot_paths: list[str] = Field(
        default_factory=list,
        description="이 검증 시도 중 저장된 스텝별 라벨링 스크린샷 경로 (logs/screenshots/ 기준 상대 경로). "
        "LLM이 채우는 필드가 아니라 verify_phase()가 내부적으로 채움",
    )
