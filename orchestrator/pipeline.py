"""Orchestration loop that wires the four agents together.

Implements the workflow from the design doc (section 3):

1. Planner splits the requirement into Phases with success criteria.
2. For each Phase, Developer <-> Reviewer <-> GUI Tester loop, retried up
   to ``max_dev_review_retries`` / ``max_gui_retries`` times with feedback
   fed back into the next Developer attempt.
3. If a Phase keeps failing past the retry limit, Planner is asked to
   replan (up to ``max_replan_attempts`` times) instead of blindly retrying
   the same approach.
4. If replanning also doesn't resolve it, Planner escalates to a human and
   resumes the loop with their feedback folded into a new replan.
5. Once every Phase has passed, Planner produces the final summary report.

The concrete agents are still stubs (see agents/*.py), so running this end
to end will raise NotImplementedError until they are filled in. The control
flow itself is complete.
"""

from __future__ import annotations

import logging

from agents.base import DeveloperAgent, GUITesterAgent, PlannerAgent, ReviewerAgent
from agents.models import DevResult, GUITestResult, Phase, PhaseStatus, ReplanContext, ReviewResult
from orchestrator.config import PipelineConfig
from orchestrator.state import StateTracker


class Orchestrator:
    def __init__(
        self,
        planner: PlannerAgent,
        developer: DeveloperAgent,
        reviewer: ReviewerAgent,
        gui_tester: GUITesterAgent,
        config: PipelineConfig,
        state: StateTracker,
        logger: logging.Logger | None = None,
    ) -> None:
        self.planner = planner
        self.developer = developer
        self.reviewer = reviewer
        self.gui_tester = gui_tester
        self.config = config
        self.state = state
        self.logger = logger or logging.getLogger("pipeline")

    def run(self, requirement: str) -> str:
        self.logger.info("Creating initial plan")
        self.state.set_requirement(requirement)
        phases = self.planner.create_plan(requirement)
        self.state.set_phases(phases)

        for phase in phases:
            self._run_phase_with_replanning(phase)

        self.logger.info("All phases finished, generating summary report")
        return self.planner.summarize_report(phases)

    def _run_phase_with_replanning(self, phase: Phase) -> None:
        dev_attempts: list[DevResult] = []
        review_attempts: list[ReviewResult] = []
        gui_attempts: list[GUITestResult] = []
        human_feedback: str | None = None

        for replan_attempt in range(self.config.max_replan_attempts + 1):
            passed = self._run_dev_review_gui_loop(
                phase, dev_attempts, review_attempts, gui_attempts, human_feedback
            )
            if passed:
                phase.status = PhaseStatus.PASSED
                self.state.update_phase_status(phase.id, phase.status)
                return

            reason = (
                f"Phase '{phase.id}' failed after "
                f"{len(dev_attempts)} dev/review/gui attempts"
            )
            context = ReplanContext(
                phase=phase,
                dev_attempts=dev_attempts,
                review_attempts=review_attempts,
                gui_attempts=gui_attempts,
                reason=reason,
                human_feedback=human_feedback,
            )

            if replan_attempt < self.config.max_replan_attempts:
                self.logger.warning("%s -- asking Planner to replan", reason)
                self.state.record_replan(phase.id, reason)
                self.planner.replan(context)
            else:
                self.logger.warning("%s -- escalating to human", reason)
                human_feedback = self.planner.request_human_escalation(context)
                self.state.record_escalation(phase.id, reason, human_feedback)
                # Give the loop one more pass with the human's feedback folded in.
                passed = self._run_dev_review_gui_loop(
                    phase, dev_attempts, review_attempts, gui_attempts, human_feedback
                )
                phase.status = PhaseStatus.PASSED if passed else PhaseStatus.ESCALATED
                self.state.update_phase_status(phase.id, phase.status)
                return

        phase.status = PhaseStatus.FAILED
        self.state.update_phase_status(phase.id, phase.status)

    def _run_dev_review_gui_loop(
        self,
        phase: Phase,
        dev_attempts: list[DevResult],
        review_attempts: list[ReviewResult],
        gui_attempts: list[GUITestResult],
        feedback: str | None,
    ) -> bool:
        phase.status = PhaseStatus.IN_PROGRESS
        self.state.update_phase_status(phase.id, phase.status)

        for attempt in range(self.config.max_dev_review_retries):
            self.logger.info("Phase %s: developer attempt %d", phase.id, attempt + 1)
            dev_result = self.developer.implement(phase, feedback=feedback)
            dev_attempts.append(dev_result)

            review_result = self.reviewer.review(phase, dev_result)
            review_attempts.append(review_result)
            if not review_result.passed:
                feedback = "\n".join(review_result.issues)
                self.logger.info("Phase %s: review failed -- %s", phase.id, feedback)
                continue

            # Action-level retry (e.g. re-attempting a single flaky click) is
            # the GUI tester agent's own concern; see MAX_GUI_RETRIES in
            # .env.example, consumed by that agent, not the orchestrator.
            gui_result = self.gui_tester.verify(phase, dev_result)
            gui_attempts.append(gui_result)
            if gui_result.passed:
                return True

            feedback = "\n".join(gui_result.issues)
            self.logger.info("Phase %s: GUI verification failed -- %s", phase.id, feedback)

        return False
