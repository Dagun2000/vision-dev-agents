"""Shared data models passed between agents and the orchestrator.

These are plain dataclasses (not tied to any single LLM provider) so that
any concrete agent implementation can be swapped in without touching the
orchestration loop or the other agents.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class PhaseStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DEV_DONE = "dev_done"
    LINT_FAILED = "lint_failed"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"


@dataclass
class Phase:
    """A single micro-unit of work produced by the Planner agent."""

    id: str
    title: str
    description: str
    success_criteria: list[str]
    status: PhaseStatus = PhaseStatus.PENDING


@dataclass
class DevResult:
    """Output of one Developer agent attempt on a Phase."""

    phase_id: str
    summary: str
    files_changed: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    """Output of the Reviewer agent's semantic review of a DevResult."""

    phase_id: str
    passed: bool
    issues: list[str] = field(default_factory=list)


@dataclass
class GUITestResult:
    """Output of the GUI Tester agent's vision-based verification."""

    phase_id: str
    passed: bool
    issues: list[str] = field(default_factory=list)
    screenshots: list[str] = field(default_factory=list)


@dataclass
class ReplanContext:
    """Everything the Planner needs to decide how to replan or escalate."""

    phase: Phase
    dev_attempts: list[DevResult] = field(default_factory=list)
    review_attempts: list[ReviewResult] = field(default_factory=list)
    gui_attempts: list[GUITestResult] = field(default_factory=list)
    reason: str = ""
    human_feedback: str | None = None
