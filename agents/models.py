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
    REVIEW_DONE = "review_done"
    REVIEW_FAILED = "review_failed"
    GUI_VERIFIED = "gui_verified"
    GUI_TEST_FAILED = "gui_test_failed"
    PASSED = "passed"
    FAILED = "failed"
    ESCALATED = "escalated"
    SKIPPED = "skipped"


class LaunchType(str, Enum):
    """How the GUI Tester should launch target-app/ to verify it.

    STATIC_WEB_SERVER, ELECTRON_APP, and PYTHON_TKINTER are implemented --
    see agents/gui_tester.py's launch_app() dispatcher. NATIVE_EXE stays an
    unimplemented generic placeholder on purpose: native targets are added
    one concrete framework at a time (own enum value, own prompt/schema),
    never lumped into one generic native slot -- see the project memory
    "Native EXE checklist" for the reasoning.
    """

    STATIC_WEB_SERVER = "static_web_server"
    NATIVE_EXE = "native_exe"
    ELECTRON_APP = "electron_app"
    PYTHON_TKINTER = "python_tkinter"


@dataclass
class LaunchConfig:
    """How to launch this Phase's app for GUI verification, as decided by
    the Developer agent (agents/schemas.py's LaunchConfigSchema is the LLM
    structured-output counterpart of this)."""

    launch_type: LaunchType
    launch_command: str
    entry_url: str


@dataclass
class Phase:
    """A single micro-unit of work produced by the Planner agent."""

    id: str
    title: str
    description: str
    success_criteria: list[str]
    status: PhaseStatus = PhaseStatus.PENDING
    launch_config: LaunchConfig | None = None


@dataclass
class DevResult:
    """Output of one Developer agent attempt on a Phase."""

    phase_id: str
    summary: str
    files_changed: list[str] = field(default_factory=list)
    launch_config: LaunchConfig | None = None
    # How many self-lint attempts it took to reach this (successful) result,
    # and the eslint/node --check messages from the failed attempts along
    # the way -- used by the development report (agents/report.py).
    lint_attempts: int = 1
    lint_errors: list[str] = field(default_factory=list)


@dataclass
class ReviewResult:
    """Output of the Reviewer agent's semantic review of a DevResult."""

    phase_id: str
    passed: bool
    issues: list[str] = field(default_factory=list)
    # How many review rounds it took to reach this result, and the issues
    # list from each rejected round along the way -- used by the
    # development report (agents/report.py).
    attempts: int = 1
    rejected_issues: list[list[str]] = field(default_factory=list)


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
