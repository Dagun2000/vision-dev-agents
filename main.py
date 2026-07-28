"""Entry point: wires the four agents into the orchestrator.

Planner, Developer and Reviewer are now backed by an LLM (agents/planner.py,
agents/developer.py, agents/reviewer.py); GUI Tester is still a stub, so
running this will raise NotImplementedError as soon as GUI verification is
requested for the first Phase -- that's expected until it's filled in.
"""

from __future__ import annotations

from agents.developer import OpenAIDeveloperAgent
from agents.gui_tester import StubGUITesterAgent
from agents.planner import OpenAIPlannerAgent
from agents.reviewer import OpenAIReviewerAgent
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
        planner=OpenAIPlannerAgent(config=config),
        developer=OpenAIDeveloperAgent(config=config),
        reviewer=OpenAIReviewerAgent(config=config),
        gui_tester=StubGUITesterAgent(),
        config=config,
        state=state,
        logger=logger,
    )

    report = orchestrator.run(REQUIREMENT)
    logger.info("Final report:\n%s", report)


if __name__ == "__main__":
    main()
