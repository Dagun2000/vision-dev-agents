"""Entry point: wires the four (currently stub) agents into the orchestrator.

Running this now will raise NotImplementedError as soon as the Planner
stub is asked to create a plan -- that's expected until agents/*.py are
filled in during the next step. The point of this script is to prove the
orchestration wiring is correct.
"""

from __future__ import annotations

from agents.developer import StubDeveloperAgent
from agents.gui_tester import StubGUITesterAgent
from agents.planner import StubPlannerAgent
from agents.reviewer import StubReviewerAgent
from orchestrator.config import PipelineConfig
from orchestrator.logging_setup import setup_logging
from orchestrator.pipeline import Orchestrator
from orchestrator.state import StateTracker

REQUIREMENT = (
    "localStorage 기반의 정적 웹 Todo 리스트 앱을 만든다. "
    "항목 추가, 완료 체크, 삭제, 새로고침 후 데이터 유지 기능을 포함한다."
)


def main() -> None:
    config = PipelineConfig()
    logger = setup_logging(config.logs_dir)
    state = StateTracker(config.state_file)

    orchestrator = Orchestrator(
        planner=StubPlannerAgent(),
        developer=StubDeveloperAgent(),
        reviewer=StubReviewerAgent(),
        gui_tester=StubGUITesterAgent(),
        config=config,
        state=state,
        logger=logger,
    )

    report = orchestrator.run(REQUIREMENT)
    logger.info("Final report:\n%s", report)


if __name__ == "__main__":
    main()
