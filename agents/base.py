"""Abstract interfaces for the four pipeline agents.

Each interface only depends on plain dataclasses from :mod:`agents.models`,
never on a specific LLM SDK. This keeps every agent independently
replaceable (e.g. swapping the OpenAI-backed Developer agent for a
Claude-backed one) without touching the orchestrator.

Concrete implementations live in ``planner.py`` / ``developer.py`` /
``reviewer.py`` / ``gui_tester.py`` and are currently stubs — the actual
LLM logic will be filled in during the next step.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from agents.models import DevResult, GUITestResult, Phase, ReplanContext, ReviewResult


class PlannerAgent(ABC):
    """Splits a requirement into Phases and drives replanning/escalation."""

    @abstractmethod
    def create_plan(self, requirement: str) -> list[Phase]:
        """Break the initial requirement into ordered, testable Phases."""
        raise NotImplementedError

    @abstractmethod
    def replan(self, context: ReplanContext) -> list[Phase]:
        """Produce a revised plan after repeated dev/review/GUI failures."""
        raise NotImplementedError

    @abstractmethod
    def request_human_escalation(self, context: ReplanContext) -> str:
        """Ask a human for guidance and return their feedback."""
        raise NotImplementedError

    @abstractmethod
    def summarize_report(self, phases: list[Phase]) -> str:
        """Produce the final development summary report."""
        raise NotImplementedError


class DeveloperAgent(ABC):
    """Writes/edits code for a Phase and self-corrects lint/AST issues."""

    @abstractmethod
    def implement(self, phase: Phase, feedback: str | None = None) -> DevResult:
        """Implement (or revise) a Phase, optionally given prior feedback."""
        raise NotImplementedError


class ReviewerAgent(ABC):
    """Performs semantic (non-syntactic) review against success criteria."""

    @abstractmethod
    def review(self, phase: Phase, dev_result: DevResult) -> ReviewResult:
        """Check dev_result against phase.success_criteria."""
        raise NotImplementedError


class GUITesterAgent(ABC):
    """Vision-based, Set-of-marks driven GUI interaction verification."""

    @abstractmethod
    def verify(self, phase: Phase, dev_result: DevResult) -> GUITestResult:
        """Drive the rendered app and check it behaves as the Phase expects."""
        raise NotImplementedError
