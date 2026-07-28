"""Manual smoke test for: the launch_config refactor + the incremental
development report + post-run browser/server cleanup, all together.

Confirms:
- Developer produces launch_config (pinned to LaunchType.STATIC_WEB_SERVER),
  persisted to state/plan.json, and GUI Tester's verify_phase() launches the
  app via the launch_app() dispatcher (agents/gui_tester.py).
- The Planner writes logs/report_*.md incrementally (agents/report.py),
  with working screenshot links.
- After the phase finishes, the browser window opened by launch_app() gets
  closed and the local server process is torn down.

Runs just one phase (whichever is first "pending" in state/plan.json), not
the whole plan, so this stays fast -- but otherwise drives the exact same
start_report -> run_phase -> finalize_report -> cleanup sequence
PlanDrivenPipeline.run_all_phases() uses.

This is NOT headless -- don't touch the mouse/keyboard while it runs.

Usage:
    uv run python scripts/smoke_test_launch_config.py
"""

from __future__ import annotations

import json

from agents.developer import OpenAIDeveloperAgent
from agents.gui.execution import enable_windows_dpi_awareness
from agents.gui_tester import OpenAIGUITesterAgent
from agents.planner import OpenAIPlannerAgent
from agents.reviewer import OpenAIReviewerAgent
from orchestrator.config import PipelineConfig
from orchestrator.encoding import ensure_utf8_stdio
from orchestrator.plan_pipeline import PlanDrivenPipeline

REQUIREMENT = (
    "localStorage 기반의 정적 웹 Todo 리스트 앱을 만든다. "
    "항목 추가, 완료 체크, 삭제, 새로고침 후 데이터 유지 기능을 포함한다."
)


def main() -> None:
    ensure_utf8_stdio()
    enable_windows_dpi_awareness()

    config = PipelineConfig()
    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    phase_dict = next(p for p in plan["phases"] if p["status"] == "pending")
    phase_id = phase_dict["id"]
    print(f"대상 Phase: {phase_id} - {phase_dict['title']}")

    planner = OpenAIPlannerAgent(config=config)
    gui_tester = OpenAIGUITesterAgent(config=config)
    pipeline = PlanDrivenPipeline(
        planner=planner,
        developer=OpenAIDeveloperAgent(config=config),
        reviewer=OpenAIReviewerAgent(config=config),
        gui_tester=gui_tester,
        config=config,
    )

    report_path = planner.start_report(REQUIREMENT)
    ok = pipeline.run_phase(phase_id)
    planner.finalize_report(pipeline._build_final_files_summary())
    gui_tester.cleanup()

    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    phase_dict = next(p for p in plan["phases"] if p["id"] == phase_id)

    print()
    print(f"run_phase({phase_id!r}) returned: {ok}")
    print(f"final status: {phase_dict['status']}")
    print(f"launch_config: {json.dumps(phase_dict.get('launch_config'), ensure_ascii=False)}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
