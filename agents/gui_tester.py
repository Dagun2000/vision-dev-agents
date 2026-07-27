"""Stub implementation of the GUI Tester agent.

TODO (next step): capture the rendered target-app, overlay Set-of-marks
labels on clickable elements, ask the vision model for "next action +
target element", drive OS-level mouse/keyboard input, and re-screenshot
only when the screen actually transitions.
"""

from __future__ import annotations

from agents.base import GUITesterAgent
from agents.models import DevResult, GUITestResult, Phase


class StubGUITesterAgent(GUITesterAgent):
    def verify(self, phase: Phase, dev_result: DevResult) -> GUITestResult:
        raise NotImplementedError("StubGUITesterAgent.verify is not implemented yet")
