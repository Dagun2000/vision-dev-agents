"""Manual smoke test for GUI Tester step 4-2: Vision judgment call.

Builds on step 4-1 (capture + detect + label) by sending the labeled
screenshot -- image only, no separate OCR text list -- plus the first
Phase's success_criteria from state/plan.json to the Vision model, and
prints back its next-action decision. Nothing is clicked/typed yet
(that's step 4-3); this only tests the judgment call itself.

This is NOT headless -- it opens your actual default browser and takes a
real screenshot of your screen with PyAutoGUI.

Usage:
    uv run python scripts/smoke_test_gui_judgment.py
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
    action, raw_path, labeled_path, boxes = gui_tester.judge_next_action(phase)

    print(f"raw screenshot: {raw_path}")
    print(f"labeled screenshot: {labeled_path}")
    print(f"감지된 요소 {len(boxes)}개")
    print()
    print("Vision 판단 결과:")
    print(f"  action: {action.action}")
    print(f"  target_element: {action.target_element}")
    print(f"  text: {action.text!r}")
    print(f"  reasoning: {action.reasoning}")


if __name__ == "__main__":
    main()
