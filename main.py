"""Entry point: Planner creates/reuses a plan, then PlanDrivenPipeline runs
every Phase through Developer -> Reviewer -> GUI Tester (with the GUI
Tester <-> Developer rewrite loop on GUI failure).

If state/plan.json already exists, its phases are resumed from whatever
status they're at instead of re-planning from scratch -- handy after a
partial run, or after testing individual agents via scripts/smoke_test_*.py.

GUI Tester drives the *real* screen (PyAutoGUI) -- don't touch the mouse/
keyboard while this runs.
"""

from __future__ import annotations

from agents.developer import OpenAIDeveloperAgent
from agents.gui_tester import OpenAIGUITesterAgent
from agents.planner import OpenAIPlannerAgent
from agents.reviewer import OpenAIReviewerAgent
from orchestrator.config import PipelineConfig
from orchestrator.logging_setup import setup_logging
from orchestrator.plan_pipeline import PlanDrivenPipeline

REQUIREMENT = (
    "localStorage 기반의 정적 웹 Todo 리스트 앱을 만든다. "
    "항목 추가, 완료 체크, 삭제, 새로고침 후 데이터 유지 기능을 포함한다."
)


def main() -> None:
    config = PipelineConfig()
    logger = setup_logging(config.logs_dir)

    planner = OpenAIPlannerAgent(config=config)
    if config.plan_file.exists():
        logger.info("Reusing existing plan at %s", config.plan_file)
    else:
        logger.info("Creating initial plan")
        planner.create_plan(REQUIREMENT)

    pipeline = PlanDrivenPipeline(
        planner=planner,
        developer=OpenAIDeveloperAgent(config=config),
        reviewer=OpenAIReviewerAgent(config=config),
        gui_tester=OpenAIGUITesterAgent(config=config),
        config=config,
    )
    pipeline.run_all_phases(REQUIREMENT)


if __name__ == "__main__":
    main()
