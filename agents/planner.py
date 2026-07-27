"""Stub implementation of the Planner agent.

TODO (next step): call the LLM to split requirements into Phases with
explicit success criteria, and to drive replanning / human escalation
when the developer<->reviewer<->gui_tester loop keeps failing.
"""

from __future__ import annotations

from agents.base import PlannerAgent
from agents.models import Phase, ReplanContext


class StubPlannerAgent(PlannerAgent):
    def create_plan(self, requirement: str) -> list[Phase]:
        raise NotImplementedError("StubPlannerAgent.create_plan is not implemented yet")

    def replan(self, context: ReplanContext) -> list[Phase]:
        raise NotImplementedError("StubPlannerAgent.replan is not implemented yet")

    def request_human_escalation(self, context: ReplanContext) -> str:
        raise NotImplementedError(
            "StubPlannerAgent.request_human_escalation is not implemented yet"
        )

    def summarize_report(self, phases: list[Phase]) -> str:
        raise NotImplementedError("StubPlannerAgent.summarize_report is not implemented yet")
