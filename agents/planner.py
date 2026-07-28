"""Planner agent implementation.

Only the initial-plan-generation responsibility (create_plan) is
implemented in this step. replan / request_human_escalation /
summarize_report are intentionally left as stubs for a later step.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict

from openai import OpenAI

from agents.base import PlannerAgent
from agents.models import Phase, PhaseStatus, ReplanContext
from agents.schemas import PlanSchema
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

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
        raise NotImplementedError("OpenAIPlannerAgent.replan is not implemented yet")

    def request_human_escalation(self, context: ReplanContext) -> str:
        raise NotImplementedError(
            "OpenAIPlannerAgent.request_human_escalation is not implemented yet"
        )

    def summarize_report(self, phases: list[Phase]) -> str:
        raise NotImplementedError("OpenAIPlannerAgent.summarize_report is not implemented yet")
