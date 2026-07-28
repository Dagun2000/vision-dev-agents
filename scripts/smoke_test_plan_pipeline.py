"""Manual smoke test for the full replan/escalation loop
(PlanDrivenPipeline.run_phase_with_replanning), using debug bug injection
to force a real, repeated GUI-verification failure without waiting for a
real bug.

Requires .env to have DEBUG_INJECT_BUG=true and DEBUG_INJECT_PHASE_ID set
to a phase that's currently "pending" (or "dev_done") in state/plan.json.
To reliably exhaust the GUI retry budget on the first injected-bug failure
(rather than relying on the Developer's rewrite failing to fix it, which
it usually doesn't), also temporarily lower MAX_GUI_TEST_RETRIES=1.

Runs just that one phase, not the whole plan: Developer implements it ->
Reviewer is skipped -> a deliberate bug is injected -> GUI Tester catches
it and (with the retry budget exhausted) the Planner is asked to replan
-> the replanned phase should implement/verify cleanly since the bug
doesn't get reintroduced.

This is NOT headless -- don't touch the mouse/keyboard while it runs. If
MAX_REPLAN_ATTEMPTS is also exhausted, this will block on console input
(the human escalation menu) -- a "yes 1 |" prefix answers any such prompt
with "1) retry" as a safety net.

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
    print(f"MAX_GUI_TEST_RETRIES={config.max_gui_test_retries} MAX_REPLAN_ATTEMPTS={config.max_replan_attempts}")
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
        outcome = pipeline.run_phase_with_replanning(phase_id)
    finally:
        planner.finalize_report(pipeline._build_final_files_summary())
        gui_tester.cleanup()

    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    replanned_phases = [p for p in plan["phases"] if p.get("replanned_from") == phase_id]

    print()
    print(f"run_phase_with_replanning({phase_id!r}) returned: {outcome}")
    original = next((p for p in plan["phases"] if p["id"] == phase_id), None)
    print(f"original phase status: {original['status'] if original else '(replaced)'}")
    for p in replanned_phases:
        print(f"replanned phase: {p['id']} status={p['status']} (from {p['replanned_from']})")
    print(f"report: {report_path}")


if __name__ == "__main__":
    main()
