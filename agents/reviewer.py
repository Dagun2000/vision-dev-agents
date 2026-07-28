"""Reviewer agent implementation.

Scope of this step: read state/plan.json, find the first Phase with
status == "dev_done", and judge -- from code + success_criteria alone,
deliberately without the Developer's own rationale -- whether the code in
target-app/ semantically satisfies those success_criteria. Lint is not
re-checked here (the Developer agent already handles that).

If rejected, the Reviewer asks the Developer to rewrite (passing back its
own issues), then re-reviews. Up to MAX_REVIEW_RETRIES rounds; if still
not approved, the Phase is marked "review_failed" in plan.json. On
approval it's marked "review_done".

GUI verification is out of scope here -- that's the GUI Tester agent's
job in a later step.
"""

from __future__ import annotations

import json
import logging

from openai import OpenAI

from agents.base import ReviewerAgent
from agents.developer import OpenAIDeveloperAgent
from agents.models import DevResult, Phase, PhaseStatus, ReviewResult
from agents.schemas import ReviewOutputSchema
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

MAX_REVIEW_RETRIES = 3
APP_FILES = ("index.html", "style.css", "app.js")

SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 코드 리뷰어(Reviewer) 에이전트입니다.
개발자가 작성한 코드가 주어진 성공 조건(success_criteria)을 "의미적으로"
만족하는지만 판단하세요.

규칙:
- 문법 오류나 코드 스타일은 신경 쓰지 마세요. Lint는 이미 개발자 에이전트가
  별도로 통과시켰습니다. 오직 "코드가 실제로 성공 조건이 요구하는 동작을
  구현하고 있는가"만 판단하세요.
- 개발자가 왜 이렇게 짰는지에 대한 설명이나 의도는 주어지지 않습니다.
  코드와 성공 조건만 보고 독립적으로 판단하세요.
- 각 성공 조건에 대해 코드에서 실제로 확인할 수 있는 근거(관련 함수/로직의
  존재 여부, 조건 분기 등)를 바탕으로 판단하세요. 근거 없이 통과시키지 마세요.
- 성공 조건 중 하나라도 코드에 구현되어 있지 않으면 approved는 false이고,
  issues에 어떤 성공 조건이 왜 충족되지 않는지 구체적으로 적으세요
  (예: "빈 입력 방지 조건: 입력값을 trim하거나 길이를 검사하는 로직이 없어
  빈 문자열도 그대로 추가됨").
- 모든 성공 조건이 충족되면 approved는 true이고 issues는 빈 배열입니다.
"""


class OpenAIReviewerAgent(ReviewerAgent):
    def __init__(
        self,
        config: PipelineConfig | None = None,
        client: OpenAI | None = None,
        developer: OpenAIDeveloperAgent | None = None,
    ) -> None:
        self.config = config or PipelineConfig()
        self.client = client or OpenAI(api_key=self.config.openai_api_key)
        self.developer = developer or OpenAIDeveloperAgent(config=self.config, client=self.client)

    # ---- ReviewerAgent interface (used by the orchestrator loop) ------

    def review(self, phase: Phase, dev_result: DevResult) -> ReviewResult:
        # dev_result is accepted for interface compliance but deliberately
        # ignored: the Reviewer must judge from code + success_criteria
        # alone, not the Developer's own account of what it did.
        current_files = self._read_current_files()
        result = self._review_files(phase, current_files)
        return ReviewResult(phase_id=phase.id, passed=result.approved, issues=result.issues)

    # ---- plan.json-driven workflow (standalone / CLI entry point) -----

    def review_next_dev_done_phase(self) -> ReviewResult:
        """Read state/plan.json, review the first 'dev_done' Phase, and
        request Developer rewrites (up to MAX_REVIEW_RETRIES rounds).
        Writes the resulting status ('review_done' / 'review_failed')
        back to plan.json."""
        plan_path = self.config.plan_file
        plan = json.loads(plan_path.read_text(encoding="utf-8"))

        phase_dict = next(
            (p for p in plan["phases"] if p["status"] == PhaseStatus.DEV_DONE.value), None
        )
        if phase_dict is None:
            raise ValueError(f"{plan_path} has no phase with status='dev_done'")

        phase = Phase(
            id=phase_dict["id"],
            title=phase_dict["title"],
            description=phase_dict["description"],
            success_criteria=phase_dict["success_criteria"],
            status=PhaseStatus.DEV_DONE,
        )

        review_result: ReviewResult | None = None
        for attempt in range(1, MAX_REVIEW_RETRIES + 1):
            current_files = self._read_current_files()
            result = self._review_files(phase, current_files)
            review_result = ReviewResult(phase_id=phase.id, passed=result.approved, issues=result.issues)

            logger.info(
                "Reviewer: phase=%s attempt=%d/%d approved=%s",
                phase.id,
                attempt,
                MAX_REVIEW_RETRIES,
                result.approved,
            )

            if result.approved:
                phase_dict["status"] = PhaseStatus.REVIEW_DONE.value
                plan_path.write_text(
                    json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                logger.info("Reviewer: phase=%s marked review_done in %s", phase.id, plan_path)
                return review_result

            logger.warning(
                "Reviewer: phase=%s rejected (attempt %d/%d) -- %s",
                phase.id,
                attempt,
                MAX_REVIEW_RETRIES,
                result.issues,
            )

            if attempt < MAX_REVIEW_RETRIES:
                logger.info("Reviewer: phase=%s requesting Developer rewrite", phase.id)
                self.developer.implement(phase, review_issues=result.issues)

        phase_dict["status"] = PhaseStatus.REVIEW_FAILED.value
        plan_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.error("Reviewer: phase=%s marked review_failed in %s", phase.id, plan_path)
        assert review_result is not None
        return review_result

    # ---- internals ------------------------------------------------------

    def _read_current_files(self) -> dict[str, str]:
        files: dict[str, str] = {}
        for name in APP_FILES:
            path = self.config.target_app_dir / name
            files[name] = path.read_text(encoding="utf-8") if path.exists() else ""
        return files

    def _review_files(self, phase: Phase, current_files: dict[str, str]) -> ReviewOutputSchema:
        user_message = self._build_user_message(phase, current_files)
        response = self.client.responses.parse(
            model=self.config.reviewer_model,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
            text_format=ReviewOutputSchema,
        )
        result = response.output_parsed
        if result is None:
            raise ValueError("Reviewer LLM returned an unparseable response")
        return result

    def _build_user_message(self, phase: Phase, current_files: dict[str, str]) -> str:
        criteria = "\n".join(f"- {c}" for c in phase.success_criteria)
        parts = [f"## 성공 조건\n{criteria}", "\n## 코드"]
        for name in APP_FILES:
            content = current_files.get(name, "")
            if content:
                parts.append(f"\n### {name}\n```\n{content}\n```")
            else:
                parts.append(f"\n### {name}\n(파일 없음)")
        return "\n".join(parts)
