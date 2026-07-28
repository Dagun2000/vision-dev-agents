"""Plan.json-driven, single-phase-at-a-time pipeline.

Runs Developer -> Reviewer -> GUI Tester for each Phase in state/plan.json,
in order. This is a different orchestration path from
orchestrator/pipeline.py's Orchestrator (which drives everything from
in-memory Phase objects handed out once by Planner.create_plan(), per the
original design doc, and replans/escalates -- that class is still the
target for the Phase 5 replanning work). This module instead treats
state/plan.json as the single source of truth for each Phase's status,
matching how every agent's plan.json-driven method
(develop_next_pending_phase / review_next_dev_done_phase / verify_phase)
already works and was tested standalone via scripts/smoke_test_*.py.
main.py uses this path so the GUI Tester feedback loop's retry counts
(review_retry_count / gui_retry_count) can live in plan.json per Phase, as
designed.

Two independent retry loops per Phase:
- Developer <-> Reviewer, handled entirely inside
  ReviewerAgent.review_next_dev_done_phase() (MAX_REVIEW_RETRIES rounds).
- Developer <-> GUI Tester, handled here (config.max_gui_test_retries
  rounds). On GUI failure, the rewrite request skips Reviewer entirely
  (re-reviewing a locally-patched-for-GUI-feedback change would just cost
  time for no benefit) and goes straight back to GUI verification.

Debug bug injection (DEBUG_INJECT_BUG / DEBUG_INJECT_PHASE_ID, see
debug/bug_injector.py) skips Reviewer for exactly one configured Phase and
sabotages the Developer's output before GUI verification, to demo the
GUI-failure -> Developer-rewrite loop without waiting for a real bug.

Console output is kept to one line per Phase start/finish (print()); the
full step-by-step trace still goes to the log file (orchestrator/logging_
setup.py routes it there, not the console). A Markdown development report
is written incrementally -- once after every Phase, not just at the end --
via the Planner agent (agents/planner.py's start_report/record_phase_
report/finalize_report, rendered by agents/report.py) so a crash mid-run
still leaves a record of everything completed so far. Once the whole run
finishes and the report is saved, the browser window / local server left
open by the last GUI verification are cleaned up (best-effort).
"""

from __future__ import annotations

import json
import logging

from agents.developer import DeveloperLintError, OpenAIDeveloperAgent
from agents.gui_tester import OpenAIGUITesterAgent
from agents.models import LaunchConfig, LaunchType, Phase, PhaseStatus
from agents.planner import OpenAIPlannerAgent
from agents.report import GUIAttemptRecord, PhaseReportRecord
from agents.reviewer import OpenAIReviewerAgent
from agents.schemas import GUITestOutputSchema
from debug.bug_injector import inject_bug
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

# Statuses that mean "this phase still needs work" vs. a terminal state
# run_all_phases() won't try to resume past.
TERMINAL_STATUSES = {
    PhaseStatus.LINT_FAILED.value,
    PhaseStatus.REVIEW_FAILED.value,
    PhaseStatus.GUI_TEST_FAILED.value,
    PhaseStatus.GUI_VERIFIED.value,
}

APP_FILES = ("index.html", "style.css", "app.js")


class PlanDrivenPipeline:
    def __init__(
        self,
        planner: OpenAIPlannerAgent,
        developer: OpenAIDeveloperAgent,
        reviewer: OpenAIReviewerAgent,
        gui_tester: OpenAIGUITesterAgent,
        config: PipelineConfig,
    ) -> None:
        self.planner = planner
        self.developer = developer
        self.reviewer = reviewer
        self.gui_tester = gui_tester
        self.config = config

    def run_all_phases(self, requirement: str) -> None:
        self.planner.start_report(requirement)

        plan = self._load_plan()
        for phase_dict in plan["phases"]:
            phase_id = phase_dict["id"]
            if phase_dict["status"] in TERMINAL_STATUSES:
                logger.info(
                    "PlanPipeline: phase=%s already at terminal status=%s, skipping",
                    phase_id,
                    phase_dict["status"],
                )
                if phase_dict["status"] != PhaseStatus.GUI_VERIFIED.value:
                    break
                continue

            print(f"[{phase_id}] 시작: {phase_dict['title']}")
            ok = self.run_phase(phase_id)
            final_status = self._load_phase_dict(phase_id)["status"]
            print(f"[{phase_id}] 완료: {final_status}")
            if not ok:
                logger.error("PlanPipeline: stopping -- phase=%s did not complete successfully", phase_id)
                break

        self.planner.finalize_report(self._build_final_files_summary())
        self.gui_tester.cleanup()

    def run_phase(self, phase_id: str) -> bool:
        """Run one Phase through Developer -> Reviewer/debug -> GUI Tester
        (+ GUI-failure -> Developer retry loop), resuming from whatever
        status it's currently at in plan.json. Records a report entry for
        the phase before returning. Returns True if the phase ends
        gui_verified, False otherwise."""
        status = self._load_phase_dict(phase_id)["status"]
        logger.info("PlanPipeline: phase=%s starting from status=%s", phase_id, status)

        dev_result = None
        review_result = None
        review_skipped_debug = False
        gui_attempts: list[GUITestOutputSchema] = []

        if status == PhaseStatus.PENDING.value:
            try:
                dev_result = self.developer.develop_next_pending_phase()
            except DeveloperLintError:
                logger.error("PlanPipeline: phase=%s stuck at lint_failed", phase_id)
                self._record_report(phase_id, review_skipped_debug=False, gui_attempts=gui_attempts)
                return False
            status = self._load_phase_dict(phase_id)["status"]

        if status == PhaseStatus.DEV_DONE.value:
            if self.config.debug_inject_bug and phase_id == self.config.debug_inject_phase_id:
                self._apply_debug_bug(phase_id)
                review_skipped_debug = True
            else:
                review_result = self.reviewer.review_next_dev_done_phase()
                if not review_result.passed:
                    logger.error("PlanPipeline: phase=%s stuck at review_failed", phase_id)
                    self._record_report(
                        phase_id, review_skipped_debug=review_skipped_debug, gui_attempts=gui_attempts
                    )
                    return False
            status = self._load_phase_dict(phase_id)["status"]

        if status == PhaseStatus.REVIEW_DONE.value:
            phase = self._load_phase(phase_id)
            ok, gui_attempts = self._gui_verify_with_retries(phase)
            self._record_report(
                phase_id, review_skipped_debug=review_skipped_debug, gui_attempts=gui_attempts
            )
            return ok

        if status == PhaseStatus.GUI_VERIFIED.value:
            logger.info("PlanPipeline: phase=%s already gui_verified", phase_id)
            self._record_report(
                phase_id, review_skipped_debug=review_skipped_debug, gui_attempts=gui_attempts
            )
            return True

        logger.error("PlanPipeline: phase=%s in unexpected status=%s, stopping", phase_id, status)
        self._record_report(phase_id, review_skipped_debug=review_skipped_debug, gui_attempts=gui_attempts)
        return False

    # ---- debug path (4-5) -----------------------------------------------

    def _apply_debug_bug(self, phase_id: str) -> None:
        app_js_path = self.config.target_app_dir / "app.js"
        original = app_js_path.read_text(encoding="utf-8")
        injected = inject_bug(phase_id, original)
        app_js_path.write_text(injected, encoding="utf-8")

        # Deliberately skip Reviewer -- go straight to GUI verification as
        # if review had passed, so the injected bug is only ever caught by
        # the GUI Tester (that's the point of this debug path).
        plan = self._load_plan()
        phase_dict = self._find_phase_dict(plan, phase_id)
        phase_dict["status"] = PhaseStatus.REVIEW_DONE.value
        phase_dict["review_skipped_debug"] = True
        self._save_plan(plan)
        logger.info("PlanPipeline: phase=%s Reviewer skipped (debug bug injection)", phase_id)

    # ---- GUI Tester <-> Developer retry loop (4-4) -----------------------

    def _gui_verify_with_retries(self, phase: Phase) -> tuple[bool, list[GUITestOutputSchema]]:
        gui_retry_count = self._load_phase_dict(phase.id).get("gui_retry_count", 0)
        attempts: list[GUITestOutputSchema] = []

        while True:
            result = self.gui_tester.verify_phase(phase)
            attempts.append(result)

            if result.success:
                self._update_phase(phase.id, status=PhaseStatus.GUI_VERIFIED, gui_retry_count=gui_retry_count)
                logger.info("PlanPipeline: phase=%s gui_verified", phase.id)
                return True, attempts

            gui_retry_count += 1
            logger.warning(
                "PlanPipeline: phase=%s GUI verification failed (attempt %d/%d) -- %s",
                phase.id,
                gui_retry_count,
                self.config.max_gui_test_retries,
                result.symptom,
            )

            if gui_retry_count >= self.config.max_gui_test_retries:
                self._update_phase(
                    phase.id, status=PhaseStatus.GUI_TEST_FAILED, gui_retry_count=gui_retry_count
                )
                logger.error(
                    "PlanPipeline: phase=%s marked gui_test_failed after %d attempt(s)",
                    phase.id,
                    gui_retry_count,
                )
                return False, attempts

            self._update_phase(phase.id, gui_retry_count=gui_retry_count)
            issues = [self._format_gui_issue(result)]
            logger.info("PlanPipeline: phase=%s requesting Developer rewrite from GUI feedback", phase.id)
            self.developer.implement(phase, review_issues=issues)
            # Loop back and re-verify -- no Reviewer pass in between.

    @staticmethod
    def _format_gui_issue(result: GUITestOutputSchema) -> str:
        if result.criterion_failed:
            return f"{result.criterion_failed}: {result.symptom}"
        return result.symptom or "GUI 검증 실패 (구체적 원인 불명)"

    # ---- development report ----------------------------------------------

    def _record_report(
        self, phase_id: str, review_skipped_debug: bool, gui_attempts: list[GUITestOutputSchema]
    ) -> None:
        phase_dict = self._load_phase_dict(phase_id)
        record = PhaseReportRecord(
            phase_id=phase_id,
            title=phase_dict["title"],
            success_criteria=phase_dict["success_criteria"],
            final_status=phase_dict["status"],
            dev_summary=phase_dict.get("dev_summary", ""),
            lint_attempts=phase_dict.get("lint_attempts", 0),
            lint_errors=phase_dict.get("lint_errors", []),
            review_attempts=(0 if review_skipped_debug else phase_dict.get("review_retry_count", 0) + 1),
            review_issues=[
                issue for issues in phase_dict.get("review_rejected_issues", []) for issue in issues
            ],
            review_skipped_debug=review_skipped_debug,
            gui_attempts=[
                GUIAttemptRecord(
                    attempt=i + 1,
                    success=attempt.success,
                    step_log=attempt.step_log,
                    symptom=attempt.symptom,
                    screenshot_paths=attempt.screenshot_paths,
                )
                for i, attempt in enumerate(gui_attempts)
            ],
        )
        self.planner.record_phase_report(record)

    def _build_final_files_summary(self) -> dict[str, str]:
        summary: dict[str, str] = {}
        for name in APP_FILES:
            path = self.config.target_app_dir / name
            if not path.exists():
                continue
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            summary[name] = f"{line_count}줄"
        return summary

    # ---- plan.json read/write helpers ------------------------------------

    def _load_plan(self) -> dict:
        return json.loads(self.config.plan_file.read_text(encoding="utf-8"))

    def _save_plan(self, plan: dict) -> None:
        self.config.plan_file.write_text(
            json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    @staticmethod
    def _find_phase_dict(plan: dict, phase_id: str) -> dict:
        phase_dict = next((p for p in plan["phases"] if p["id"] == phase_id), None)
        if phase_dict is None:
            raise ValueError(f"plan.json has no phase with id={phase_id!r}")
        return phase_dict

    def _load_phase_dict(self, phase_id: str) -> dict:
        return self._find_phase_dict(self._load_plan(), phase_id)

    def _load_phase(self, phase_id: str) -> Phase:
        phase_dict = self._load_phase_dict(phase_id)
        launch_config_dict = phase_dict.get("launch_config")
        launch_config = (
            LaunchConfig(
                launch_type=LaunchType(launch_config_dict["launch_type"]),
                launch_command=launch_config_dict["launch_command"],
                entry_url=launch_config_dict["entry_url"],
            )
            if launch_config_dict
            else None
        )
        return Phase(
            id=phase_dict["id"],
            title=phase_dict["title"],
            description=phase_dict["description"],
            success_criteria=phase_dict["success_criteria"],
            status=PhaseStatus(phase_dict["status"]),
            launch_config=launch_config,
        )

    def _update_phase(self, phase_id: str, status: PhaseStatus | None = None, **fields: object) -> None:
        plan = self._load_plan()
        phase_dict = self._find_phase_dict(plan, phase_id)
        if status is not None:
            phase_dict["status"] = status.value
        phase_dict.update(fields)
        self._save_plan(plan)
