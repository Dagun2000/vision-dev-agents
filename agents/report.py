"""Development report data model + Markdown rendering.

Kept separate from agents/planner.py so the (fairly mechanical) rendering
logic can be read/reviewed on its own. OpenAIPlannerAgent
(agents/planner.py) owns *when* to write the report (once at the start of
a run, again after every Phase, and once more at the end) -- this module
just knows how to turn accumulated PhaseReportRecords into Markdown.

Deliberately not an LLM call: every field here is already-known structured
data (attempt counts, issues lists, step logs, file paths), so generating
prose through a model would only add latency and hallucination risk for
no benefit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from agents.schemas import GUIStepLogEntry

TERMINAL_FAILURE_STATUSES = {"lint_failed", "review_failed", "gui_test_failed"}


@dataclass
class GUIAttemptRecord:
    """One GUI Tester verify_phase() call's outcome, for the report."""

    attempt: int
    success: bool
    step_log: list[GUIStepLogEntry] = field(default_factory=list)
    symptom: str | None = None
    screenshot_paths: list[str] = field(default_factory=list)


@dataclass
class PhaseReportRecord:
    """Everything the report needs about one Phase's run."""

    phase_id: str
    title: str
    success_criteria: list[str]
    final_status: str
    dev_summary: str = ""
    lint_attempts: int = 0
    lint_errors: list[str] = field(default_factory=list)
    review_attempts: int = 0
    review_issues: list[str] = field(default_factory=list)
    review_skipped_debug: bool = False
    gui_attempts: list[GUIAttemptRecord] = field(default_factory=list)
    replanned: bool = False
    replan_notes: str = ""
    human_escalated: bool = False
    human_feedback: str = ""


def _retry_count(attempts: int) -> int:
    """attempts=1 means it succeeded on the first try -- 0 retries."""
    return max(attempts - 1, 0)


def render_report_markdown(
    requirement: str,
    phases: list[PhaseReportRecord],
    started_at: datetime,
    final_files_summary: dict[str, str] | None = None,
) -> str:
    total = len(phases)
    succeeded = sum(1 for p in phases if p.final_status == "gui_verified")
    failed = sum(1 for p in phases if p.final_status in TERMINAL_FAILURE_STATUSES)

    total_retries = 0
    for p in phases:
        total_retries += _retry_count(p.lint_attempts)
        total_retries += _retry_count(p.review_attempts)
        total_retries += _retry_count(len(p.gui_attempts))

    replans = sum(1 for p in phases if p.replanned)
    escalations = sum(1 for p in phases if p.human_escalated)
    elapsed = datetime.now() - started_at

    lines: list[str] = []
    lines.append("# 개발 요약 보고서")
    lines.append("")
    lines.append("## 1. 요구사항")
    lines.append("")
    lines.append(requirement)
    lines.append("")
    lines.append("## 2. 실행 개요")
    lines.append("")
    lines.append(f"- 총 Phase 수: {total}")
    lines.append(f"- 성공: {succeeded}개 / 실패: {failed}개 / 진행 중 또는 미착수: {total - succeeded - failed}개")
    lines.append(f"- 총 소요 시간: {elapsed}")
    lines.append(f"- 총 재시도 횟수: {total_retries}")
    lines.append(f"- 재계획 발생 횟수: {replans}")
    lines.append(f"- 사람 개입 발생 횟수: {escalations}")
    lines.append("")
    lines.append("## 3. Phase별 상세 기록")
    lines.append("")

    for p in phases:
        lines.append(f"### {p.phase_id}: {p.title}")
        lines.append("")
        lines.append("**성공 조건**")
        for criterion in p.success_criteria:
            lines.append(f"- {criterion}")
        lines.append("")
        lines.append(f"**최종 상태**: `{p.final_status}`")
        lines.append("")
        if p.dev_summary:
            lines.append(f"**개발자 코드 요약**: {p.dev_summary}")
            lines.append("")

        lines.append(f"**Lint**: {p.lint_attempts}회 시도 후 통과 (재시도 {_retry_count(p.lint_attempts)}회)")
        if p.lint_errors:
            lines.append("")
            lines.append("잡혔던 에러:")
            for error in p.lint_errors:
                for error_line in error.splitlines():
                    lines.append(f"- {error_line}")
        lines.append("")

        if p.review_skipped_debug:
            lines.append("**Reviewer**: 디버그 버그 주입 경로로 건너뜀 (`review_skipped_debug=true`)")
        else:
            lines.append(
                f"**Reviewer**: {p.review_attempts}회 시도 후 승인 (재시도 {_retry_count(p.review_attempts)}회)"
            )
            if p.review_issues:
                lines.append("")
                lines.append("반려됐던 issues:")
                for issue in p.review_issues:
                    lines.append(f"- {issue}")
        lines.append("")

        lines.append("**GUI 검증**")
        lines.append("")
        for gui_attempt in p.gui_attempts:
            result_label = "성공" if gui_attempt.success else "실패"
            lines.append(f"- 시도 {gui_attempt.attempt}: {result_label} ({len(gui_attempt.step_log)} 스텝)")
            for entry in gui_attempt.step_log:
                lines.append(f"  {entry.step}. {entry.action} -> {entry.result}")
            if not gui_attempt.success and gui_attempt.symptom:
                lines.append(f"  - symptom: {gui_attempt.symptom}")
            for shot in gui_attempt.screenshot_paths:
                lines.append(f"  ![{p.phase_id} 시도{gui_attempt.attempt} 스크린샷]({shot})")
        lines.append("")

        if p.replanned:
            lines.append(f"**재계획**: {p.replan_notes}")
            lines.append("")
        if p.human_escalated:
            lines.append(f"**사람 개입**: {p.human_feedback}")
            lines.append("")

    lines.append("## 4. 최종 산출물")
    lines.append("")
    if final_files_summary:
        for name, summary in final_files_summary.items():
            lines.append(f"- `{name}`: {summary}")
    else:
        lines.append("(파이프라인이 아직 진행 중입니다)")
    lines.append("")

    lines.append("## 5. 알려진 이슈 / 남은 과제")
    lines.append("")
    unresolved = [p for p in phases if p.final_status in TERMINAL_FAILURE_STATUSES]
    if unresolved:
        for p in unresolved:
            lines.append(f"- {p.phase_id} ({p.title}): `{p.final_status}` 상태로 종료됨")
    else:
        lines.append("없음")
    lines.append("")

    return "\n".join(lines)
