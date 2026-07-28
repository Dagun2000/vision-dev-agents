"""Planner agent implementation: create_plan, replan, human escalation, and
the incremental development report.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from openai import OpenAI

from agents.base import PlannerAgent
from agents.models import Phase, PhaseStatus, ReplanContext
from agents.report import PhaseReportRecord, render_report_markdown
from agents.schemas import PlanSchema
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

APP_FILES = ("index.html", "style.css", "app.js")

REPLAN_SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 기획자(Planner) 에이전트입니다.
아래 Phase가 개발자<->리뷰어 또는 개발자<->GUI검증 루프를 반복해도 통과하지
못했습니다. 같은 접근을 그대로 반복시키는 재계획은 의미가 없습니다.

다음 두 전략 중 실패 이력에 비추어 더 적절한 쪽을 선택해 새로운 Phase
목록을 만드세요 (실패한 Phase 하나를 이 목록으로 대체합니다):
1. Phase를 더 작은 단위로 쪼개기 -- 각 단위가 독립적으로 구현·검증 가능하도록
   (예: "완료 체크 + 삭제"를 "완료 체크"와 "삭제"로 분리)
2. 요구사항 범위를 좁히거나 다른 구현 방식으로 우회하기 -- 반복 실패의
   근본 원인(예: 특정 UI 패턴, 특정 상호작용 방식)을 피하는 다른 방식 제안

규칙:
- id는 "<원래 Phase id>-a", "<원래 Phase id>-b"... 형식으로 접미사를 붙여
  기존 id와 충돌하지 않게 하세요.
- status는 항상 "pending"으로 설정하세요.
- 이미 완료된 이전 Phase들의 누적 코드 위에서 이어서 작업해야 하므로, 그
  코드와 상충하지 않는 계획을 세우세요.
- success_criteria는 화면에서 관찰 가능한 구체적인 결과로 작성하세요.
"""

SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 기획자(Planner) 에이전트입니다.
사용자가 자연어로 설명한 앱 요구사항을 받아, 개발자 에이전트가 순서대로
구현할 수 있는 독립적인 Phase(작업 단위)들로 분할하세요.

전제:
- 결과물은 브라우저에서 동작하는 localStorage 기반 정적 웹앱(HTML/CSS/JS)입니다.
  별도의 백엔드 서버나 빌드 도구는 없습니다.
- 이후 코드 리뷰어 에이전트와 GUI 검증 에이전트가 각 Phase의 success_criteria만
  보고 통과/실패를 판정합니다.

규칙:
1. 각 Phase는 이전 Phase가 완료된 상태 위에서 이어서 작업할 수 있도록 순서를
   정하세요 (예: 기본 골격/렌더링 -> 항목 추가 -> 완료 체크 -> 삭제 ->
   localStorage 저장/복원 -> 입력 검증 등).
2. success_criteria는 화면에서 관찰 가능한 구체적인 결과로 작성하세요.
   - 나쁜 예: "UI가 잘 동작해야 한다"
   - 좋은 예: "입력창에 텍스트를 입력하고 추가 버튼을 클릭하면 리스트
     최하단에 해당 텍스트를 가진 새 항목이 즉시 표시되어야 한다"
   각 Phase마다 성공 조건을 1개 이상 작성하세요.
3. id는 "phase-1", "phase-2"... 형식의 순번을 사용하세요.
4. status는 항상 "pending"으로 설정하세요.
5. Phase 개수는 요구사항 범위에 맞게 필요한 만큼만 만드세요 (일반적으로 3~7개).
   요구사항에 없는 기능을 임의로 추가하지 마세요.
"""


class OpenAIPlannerAgent(PlannerAgent):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        client: OpenAI | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.client = client or OpenAI(api_key=self.config.openai_api_key)
        self._report_requirement: str = ""
        self._report_started_at: datetime | None = None
        self._report_path: Path | None = None
        self._report_phases: list[PhaseReportRecord] = []
        self._report_final_files: dict[str, str] | None = None

    def create_plan(self, requirement: str) -> list[Phase]:
        logger.info("Planner: requesting plan from model=%s", self.config.planner_model)
        response = self.client.responses.parse(
            model=self.config.planner_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": requirement},
            ],
            text_format=PlanSchema,
        )
        plan = response.output_parsed
        if plan is None or not plan.phases:
            raise ValueError("Planner returned an empty or unparseable plan")

        phases = [
            Phase(
                id=item.id,
                title=item.title,
                description=item.description,
                success_criteria=item.success_criteria,
                status=PhaseStatus.PENDING,
            )
            for item in plan.phases
        ]
        self._save_plan(phases)
        logger.info("Planner: created %d phase(s), saved to %s", len(phases), self.config.plan_file)
        return phases

    def _save_plan(self, phases: list[Phase]) -> None:
        self.config.plan_file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"phases": [asdict(phase) for phase in phases]}
        self.config.plan_file.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def replan(self, context: ReplanContext) -> list[Phase]:
        """Ask the model to replace context.phase with 1+ new Phases,
        given its full failure history -- not just "try again", but an
        explicit instruction to split scope or change approach. Splices
        the result into plan.json in place of the old phase and returns
        the new Phase objects (the caller resumes from new_phases[0])."""
        logger.info("Planner: replanning phase=%s reason=%s", context.phase.id, context.reason)
        user_message = self._build_replan_message(context)
        response = self.client.responses.parse(
            model=self.config.planner_model,
            input=[
                {"role": "system", "content": REPLAN_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            text_format=PlanSchema,
        )
        plan = response.output_parsed
        if plan is None or not plan.phases:
            raise ValueError("Planner returned an empty or unparseable replan")

        new_phases = [
            Phase(
                id=item.id,
                title=item.title,
                description=item.description,
                success_criteria=item.success_criteria,
                status=PhaseStatus.PENDING,
            )
            for item in plan.phases
        ]
        self._splice_phases(context.phase.id, new_phases)
        logger.info(
            "Planner: replan produced %d phase(s) replacing %s: %s",
            len(new_phases),
            context.phase.id,
            [p.id for p in new_phases],
        )
        return new_phases

    def _build_replan_message(self, context: ReplanContext) -> str:
        criteria = "\n".join(f"- {c}" for c in context.phase.success_criteria)
        review_issues_text = (
            "\n".join(
                f"{i + 1}차 리뷰 반려 사유: {', '.join(r.issues) or '(사유 없음)'}"
                for i, r in enumerate(context.review_attempts)
            )
            or "(리뷰 실패 없음)"
        )
        gui_issues_text = (
            "\n".join(
                f"GUI 검증 시도 {i + 1}: {', '.join(g.issues) or '(사유 없음)'}"
                for i, g in enumerate(context.gui_attempts)
            )
            or "(GUI 검증 실패 없음)"
        )

        parts = [
            f"## 실패한 Phase\nid: {context.phase.id}\n제목: {context.phase.title}\n설명: {context.phase.description}",
            f"\n## 성공 조건\n{criteria}",
            f"\n## 실패 사유\n{context.reason}",
            f"\n## 리뷰 실패 이력\n{review_issues_text}",
            f"\n## GUI 검증 실패 이력\n{gui_issues_text}",
        ]
        if context.human_feedback:
            parts.append(f"\n## 사람이 제시한 방향 (반드시 반영할 것)\n{context.human_feedback}")

        files_text = "\n".join(
            f"### {name}\n```\n{content}\n```"
            for name, content in self._read_current_target_app_files().items()
            if content
        )
        parts.append(f"\n## 현재까지 누적된 target-app/ 코드 (이전 Phase까지 완료된 상태)\n{files_text}")
        return "\n".join(parts)

    def _read_current_target_app_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in APP_FILES:
            path = self.config.target_app_dir / name
            files[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return files

    def _splice_phases(self, old_phase_id: str, new_phases: list[Phase]) -> None:
        plan_data = json.loads(self.config.plan_file.read_text(encoding="utf-8"))
        index = next(
            (i for i, p in enumerate(plan_data["phases"]) if p["id"] == old_phase_id), None
        )
        if index is None:
            raise ValueError(f"plan.json has no phase with id={old_phase_id!r}")

        new_dicts = [asdict(phase) for phase in new_phases]
        plan_data["phases"][index : index + 1] = new_dicts
        self.config.plan_file.write_text(
            json.dumps(plan_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def request_human_escalation(self, context: ReplanContext) -> str:
        """Print a situation summary + menu, block on console input, and
        return one of a small fixed vocabulary the caller (PlanDrivenPipeline)
        parses: "RETRY", "SKIP", "ABORT", or "CUSTOM: <free-text guidance>".

        The PlannerAgent ABC's request_human_escalation() -> str contract
        predates this menu design; encoding the choice as a prefixed string
        keeps that contract instead of widening the interface for one caller.
        """
        print()
        print(f">>> Phase {context.phase.id} 재계획 후에도 {context.reason}")
        print("    [1] 이대로 한 번 더 재시도   [2] 수정 방향 직접 입력")
        print("    [3] 이 Phase 스킵하고 계속 진행   [4] 전체 중단")
        choice = input(">>> 선택 (1-4): ").strip()

        if choice == "2":
            feedback = input(">>> 수정 방향을 입력하세요: ").strip()
            logger.info("Planner: human escalation phase=%s choice=CUSTOM", context.phase.id)
            return f"CUSTOM: {feedback}"
        if choice == "3":
            logger.info("Planner: human escalation phase=%s choice=SKIP", context.phase.id)
            return "SKIP"
        if choice == "4":
            logger.info("Planner: human escalation phase=%s choice=ABORT", context.phase.id)
            return "ABORT"

        logger.info("Planner: human escalation phase=%s choice=RETRY", context.phase.id)
        return "RETRY"

    def summarize_report(self, phases: list[Phase]) -> str:
        raise NotImplementedError(
            "OpenAIPlannerAgent.summarize_report is not implemented yet -- "
            "use start_report()/record_phase_report()/finalize_report() instead, "
            "which already produce the incremental Markdown development report"
        )

    # ---- incremental development report ---------------------------------
    #
    # Written to disk after every Phase (not just once at the end) so a
    # crash mid-run still leaves a record of everything completed so far.

    def start_report(self, requirement: str) -> Path:
        self._report_requirement = requirement
        self._report_started_at = datetime.now()
        self._report_phases = []
        self._report_final_files = None
        self._report_path = (
            self.config.logs_dir / f"report_{self._report_started_at:%Y%m%d_%H%M%S}.md"
        )
        self._write_report()
        logger.info("Planner: report initialized at %s", self._report_path)
        return self._report_path

    def record_phase_report(self, record: PhaseReportRecord) -> None:
        if self._report_started_at is None:
            raise RuntimeError("start_report() must be called before record_phase_report()")
        self._report_phases.append(record)
        self._write_report()
        logger.info("Planner: report updated (phase=%s) at %s", record.phase_id, self._report_path)

    def finalize_report(self, final_files_summary: dict[str, str] | None = None) -> Path:
        if self._report_path is None:
            raise RuntimeError("start_report() must be called before finalize_report()")
        self._report_final_files = final_files_summary
        self._write_report()
        print(f"리포트 생성 완료: {self._report_path}")
        return self._report_path

    def _write_report(self) -> None:
        assert self._report_path is not None and self._report_started_at is not None
        markdown = render_report_markdown(
            requirement=self._report_requirement,
            phases=self._report_phases,
            started_at=self._report_started_at,
            final_files_summary=self._report_final_files,
        )
        self._report_path.parent.mkdir(parents=True, exist_ok=True)
        self._report_path.write_text(markdown, encoding="utf-8")
