"""Entry point: the full multi-agent pipeline, end to end.

1. Reads the requirement from config/requirement.txt.
2. Planner creates a plan (or reuses state/plan.json if it already exists
   -- handy after a partial run, or after testing individual agents via
   scripts/smoke_test_*.py).
3. PlanDrivenPipeline walks every Phase in order: Developer -> Reviewer ->
   GUI Tester, with the GUI Tester <-> Developer rewrite loop on GUI
   failure. If a Phase exhausts its review/GUI retry budget, the Planner
   replans it (smaller scope / different approach) up to
   MAX_REPLAN_ATTEMPTS times; if it's still stuck after that, a human is
   asked on the console (retry / give direction / skip / abort).
4. A Markdown development report is written incrementally to logs/, and
   the browser window / local server are cleaned up at the end.

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


def main() -> None:
    config = PipelineConfig()
    logger = setup_logging(config.logs_dir)

    if not config.requirement_file.exists():
        raise SystemExit(
            f"{config.requirement_file} not found -- write the app requirement there first"
        )
    requirement = config.requirement_file.read_text(encoding="utf-8").strip()
    if not requirement:
        raise SystemExit(f"{config.requirement_file} is empty")

    planner = OpenAIPlannerAgent(config=config)
    if config.plan_file.exists():
        logger.info("Reusing existing plan at %s", config.plan_file)
    else:
        logger.info("Creating initial plan from %s", config.requirement_file)
        planner.create_plan(requirement)

    pipeline = PlanDrivenPipeline(
        planner=planner,
        developer=OpenAIDeveloperAgent(config=config),
        reviewer=OpenAIReviewerAgent(config=config),
        gui_tester=OpenAIGUITesterAgent(config=config),
        config=config,
    )
    pipeline.run_all_phases(requirement)


if __name__ == "__main__":
    main()
