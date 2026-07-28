"""Manual smoke test for step 4-4: the plan.json-driven pipeline's GUI
Tester <-> Developer rewrite loop, using debug bug injection (step 4-5) to
force a real GUI-verification failure without waiting for a real bug.

Requires .env to have DEBUG_INJECT_BUG=true and DEBUG_INJECT_PHASE_ID set
to a phase that's currently "pending" (or "dev_done") in state/plan.json.
Runs just that one phase (PlanDrivenPipeline.run_phase), not the whole
plan, so this stays fast: Developer implements it -> Reviewer is skipped
-> a deliberate bug is injected -> GUI Tester should catch it and fail ->
Developer rewrites from that feedback -> GUI Tester should pass this time.

This is NOT headless -- don't touch the mouse/keyboard while it runs.

Usage:
    uv run python scripts/smoke_test_plan_pipeline.py
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
    print(f"DEBUG_INJECT_BUG={config.debug_inject_bug} DEBUG_INJECT_PHASE_ID={config.debug_inject_phase_id}")
    if not config.debug_inject_bug or not config.debug_inject_phase_id:
        raise SystemExit("Set DEBUG_INJECT_BUG=true and DEBUG_INJECT_PHASE_ID in .env before running this.")

    planner = OpenAIPlannerAgent(config=config)
    gui_tester = OpenAIGUITesterAgent(config=config)
    pipeline = PlanDrivenPipeline(
        planner=planner,
        developer=OpenAIDeveloperAgent(config=config),
        reviewer=OpenAIReviewerAgent(config=config),
        gui_tester=gui_tester,
        config=config,
    )

    phase_id = config.debug_inject_phase_id
    report_path = planner.start_report(REQUIREMENT)
    try:
        ok = pipeline.run_phase(phase_id)
    finally:
        planner.finalize_report(pipeline._build_final_files_summary())
        gui_tester.cleanup()

    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    phase_dict = next(p for p in plan["phases"] if p["id"] == phase_id)

    print()
    print(f"run_phase({phase_id!r}) returned: {ok}")
    print(f"final status: {phase_dict['status']}")
    print(f"gui_retry_count: {phase_dict.get('gui_retry_count')}")
    print(f"review_skipped_debug: {phase_dict.get('review_skipped_debug')}")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
