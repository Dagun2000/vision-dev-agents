"""Manual smoke test for GUI Tester step 4-3: full capture -> judge -> act
-> re-verify loop (OpenAIGUITesterAgent.verify_phase()).

Runs the first Phase from state/plan.json end to end: opens the real
default browser (fresh reload, so any manually-entered test data is
cleared), then repeatedly screenshots, asks Vision for the next action,
executes it for real via PyAutoGUI, and checks whether the screen
changed -- until the model reports success/failure or MAX_GUI_STEPS is
hit. Prints the resulting GUITestOutputSchema (step_log included).

This does NOT touch plan.json's status field (that's step 4-4).

This is NOT headless -- don't touch the mouse/keyboard while it runs.

Usage:
    uv run python scripts/smoke_test_gui_verify.py
"""

from __future__ import annotations

import json

from agents.gui.execution import enable_windows_dpi_awareness
from agents.gui_tester import OpenAIGUITesterAgent
from agents.models import Phase, PhaseStatus
from orchestrator.config import PipelineConfig
from orchestrator.encoding import ensure_utf8_stdio


def _load_first_phase(config: PipelineConfig) -> Phase:
    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    phase_dict = plan["phases"][0]
    return Phase(
        id=phase_dict["id"],
        title=phase_dict["title"],
        description=phase_dict["description"],
        success_criteria=phase_dict["success_criteria"],
        status=PhaseStatus(phase_dict["status"]),
    )


def main() -> None:
    ensure_utf8_stdio()
    enable_windows_dpi_awareness()

    config = PipelineConfig()
    phase = _load_first_phase(config)
    print(f"대상 Phase: {phase.id} - {phase.title}")
    for criterion in phase.success_criteria:
        print(f"  - {criterion}")
    print()

    gui_tester = OpenAIGUITesterAgent(config=config)
    result = gui_tester.verify_phase(phase)

    print()
    print(f"success: {result.success}")
    print(f"criterion_failed: {result.criterion_failed}")
    print(f"symptom: {result.symptom}")
    print()
    print("step_log:")
    for entry in result.step_log:
        print(f"  {entry.step}. {entry.action} -> {entry.result}")


if __name__ == "__main__":
    main()
