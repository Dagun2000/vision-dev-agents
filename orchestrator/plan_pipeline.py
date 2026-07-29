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
from agents.models import (
    GUITestResult,
    LaunchConfig,
    LaunchType,
    Phase,
    PhaseStatus,
    ReplanContext,
    ReviewResult,
)
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
    PhaseStatus.SKIPPED.value,
}

APP_FILES = ("index.html", "style.css", "app.js")
# Electron-only extra files (see agents/developer.py's ELECTRON_FILES) --
# only used for the final report's file listing, which just skips whatever
# doesn't exist, so this is a no-op for static_web_server runs.
ELECTRON_FILES = ("main.js", "package.json")


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
        resumable_statuses = {PhaseStatus.GUI_VERIFIED.value, PhaseStatus.SKIPPED.value}

        plan = self._load_plan()
        for phase_dict in plan["phases"]:
            phase_id = phase_dict["id"]
            if phase_dict["status"] in TERMINAL_STATUSES:
                logger.info(
                    "PlanPipeline: phase=%s already at terminal status=%s, skipping",
                    phase_id,
                    phase_dict["status"],
                )
                if phase_dict["status"] not in resumable_statuses:
                    break
                continue

            print(f"[{phase_id}] 시작: {phase_dict['title']}")
            outcome = self.run_phase_with_replanning(phase_id)
            final_status = self._load_phase_dict(phase_id)["status"]
            print(f"[{phase_id}] 완료: {final_status} ({outcome})")

            if outcome == "aborted":
                logger.error("PlanPipeline: stopping -- human chose to abort at phase=%s", phase_id)
                break

        self.planner.finalize_report(self._build_final_files_summary())
        self.gui_tester.cleanup()

    # ---- replan + human escalation (Phase 5) ------------------------------

    def run_phase_with_replanning(self, phase_id: str) -> str:
        """Run a Phase via run_phase(); if it exhausts its own review/GUI
        retry budgets, ask the Planner to replan (up to
        config.max_replan_attempts times) -- explicitly telling it not to
        repeat the same approach -- instead of just giving up. If it still
        fails after replanning, escalate to a human on the console.

        replan() can split one Phase into several -- all of them get
        processed (each with its own fresh replan budget) before this
        returns, not just the first. Returns "success" (all of them did),
        "skipped" (at least one was skipped by a human but the rest still
        got processed), or "aborted" (a human stopped everything, possibly
        leaving some split-off phases untouched)."""
        queue = [phase_id]
        saw_skip = False

        while queue:
            current_phase_id = queue.pop(0)
            outcome, followups = self._run_single_phase_with_replanning(current_phase_id)
            if outcome == "aborted":
                return "aborted"
            if outcome == "skipped":
                saw_skip = True
            queue = followups + queue

        return "skipped" if saw_skip else "success"

    def _run_single_phase_with_replanning(self, phase_id: str) -> tuple[str, list[str]]:
        """Handles one phase_id's own replan+escalation loop (its own
        fresh replan_round budget). Returns (outcome, followup_phase_ids)
        -- followups are any additional Phases a replan() call produced
        besides the one actually retried (current_phase_id always follows
        new_phases[0]; the rest queue up for the caller)."""
        current_phase_id = phase_id
        replan_round = 0
        human_feedback: str | None = None
        followups: list[str] = []
        # Captured before any replan splices phase_id out of plan.json, so
        # there's still something to describe "the original approach" with
        # once a replanned phase succeeds -- see _record_lesson().
        original_phase = self._load_phase(phase_id)
        last_reason: str | None = None

        while True:
            ok = self.run_phase(current_phase_id)
            if ok:
                if replan_round > 0:
                    self._record_lesson(original_phase, last_reason, current_phase_id)
                return "success", followups

            reason = self._describe_failure(current_phase_id)
            last_reason = reason

            if replan_round < self.config.max_replan_attempts:
                replan_round += 1
                print(
                    f"[{current_phase_id}] 재계획 시도 {replan_round}/{self.config.max_replan_attempts}: {reason}"
                )
                context = self._build_replan_context(current_phase_id, reason, human_feedback)
                human_feedback = None
                new_phase_ids = self._replan_from(current_phase_id, context)
                current_phase_id = new_phase_ids[0]
                followups = new_phase_ids[1:] + followups
                continue

            context = self._build_replan_context(current_phase_id, reason, human_feedback)
            outcome = self.planner.request_human_escalation(context)
            self._update_phase(current_phase_id, human_escalated=True, human_escalation_choice=outcome)

            if outcome == "ABORT":
                print(f"[{current_phase_id}] 사람이 전체 중단을 선택했습니다.")
                return "aborted", followups
            if outcome == "SKIP":
                self._update_phase(current_phase_id, status=PhaseStatus.SKIPPED)
                print(f"[{current_phase_id}] 사람이 스킵을 선택했습니다.")
                return "skipped", followups
            if outcome.startswith("CUSTOM: "):
                human_feedback = outcome[len("CUSTOM: ") :]
                replan_round = 0  # human guidance gets its own fresh replan budget
                print(f"[{current_phase_id}] 사람이 방향을 제시했습니다 -- 재계획을 다시 시도합니다.")
                continue

            # "RETRY" -- try the exact same phase again from scratch. Doesn't
            # reset replan_round, so a second failure re-escalates instead of
            # silently burning through another automatic replan round.
            print(f"[{current_phase_id}] 사람이 재시도를 선택했습니다.")
            self._update_phase(current_phase_id, status=PhaseStatus.PENDING)
            continue

    def _replan_from(self, phase_id: str, context: ReplanContext) -> list[str]:
        new_phases = self.planner.replan(context)
        for new_phase in new_phases:
            self._update_phase(
                new_phase.id, replanned_from=phase_id, replan_reason=context.reason
            )
        return [p.id for p in new_phases]

    def _record_lesson(self, original_phase: Phase, reason: str | None, new_phase_id: str) -> None:
        """Best-effort: called only once a replanned Phase actually passes.
        planner.append_lesson() already swallows its own failures, so this
        never risks the pipeline's own success/failure over a logging step."""
        new_phase = self._load_phase(new_phase_id)
        # Labeled fields rather than one flowing sentence -- this is only
        # ever read back in by an LLM (create_plan()'s prompt), not a
        # human, so structured beats grammatically smooth.
        summary = (
            f"- 시도한 접근: {original_phase.description}\n"
            f"- 실패 원인: {reason or '반복 실패'}\n"
            f"- 성공한 접근: {new_phase.title} -- {new_phase.description}"
        )
        self.planner.append_lesson(original_phase, summary)

    def _describe_failure(self, phase_id: str) -> str:
        phase_dict = self._load_phase_dict(phase_id)
        status = phase_dict["status"]
        if status == PhaseStatus.LINT_FAILED.value:
            return f"Lint 실패 ({phase_dict.get('lint_attempts', '?')}회 시도)"
        if status == PhaseStatus.REVIEW_FAILED.value:
            return f"코드 리뷰 실패 (재시도 {phase_dict.get('review_retry_count', '?')}회)"
        if status == PhaseStatus.GUI_TEST_FAILED.value:
            symptom = phase_dict.get("gui_last_symptom") or ""
            return f"GUI 검증 실패 (재시도 {phase_dict.get('gui_retry_count', '?')}회) -- {symptom}"
        return f"알 수 없는 실패 상태: {status}"

    def _build_replan_context(
        self, phase_id: str, reason: str, human_feedback: str | None
    ) -> ReplanContext:
        phase_dict = self._load_phase_dict(phase_id)
        phase = self._load_phase(phase_id)

        review_attempts = [
            ReviewResult(phase_id=phase_id, passed=False, issues=issues)
            for issues in phase_dict.get("review_rejected_issues", [])
        ]

        gui_attempts: list[GUITestResult] = []
        gui_retry_count = phase_dict.get("gui_retry_count", 0)
        if gui_retry_count:
            last_symptom = phase_dict.get("gui_last_symptom") or ""
            gui_attempts = [
                GUITestResult(
                    phase_id=phase_id,
                    passed=False,
                    issues=[f"(총 {gui_retry_count}회 실패) {last_symptom}"],
                )
            ]

        return ReplanContext(
            phase=phase,
            review_attempts=review_attempts,
            gui_attempts=gui_attempts,
            reason=reason,
            human_feedback=human_feedback,
        )

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
            if self.config.debug_inject_bug and phase_id in self.config.debug_inject_phase_ids:
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
        if injected == original:
            logger.warning(
                "PlanPipeline: phase=%s debug bug injection was a no-op -- this "
                "phase's app.js didn't match the injector's expected code pattern "
                "(e.g. no add-Todo submit handler yet), so Reviewer is still being "
                "skipped but nothing was actually broken. GUI verification will "
                "likely just pass. Pick a DEBUG_INJECT_PHASE_ID phase that actually "
                "implements the add flow.",
                phase_id,
            )
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
                    phase.id,
                    status=PhaseStatus.GUI_TEST_FAILED,
                    gui_retry_count=gui_retry_count,
                    gui_last_symptom=result.symptom,
                    gui_last_criterion_failed=result.criterion_failed,
                )
                logger.error(
                    "PlanPipeline: phase=%s marked gui_test_failed after %d attempt(s)",
                    phase.id,
                    gui_retry_count,
                )
                return False, attempts

            self._update_phase(
                phase.id,
                gui_retry_count=gui_retry_count,
                gui_last_symptom=result.symptom,
                gui_last_criterion_failed=result.criterion_failed,
            )
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
            replanned=bool(phase_dict.get("replanned_from")),
            replan_notes=(
                f"'{phase_dict['replanned_from']}' Phase가 반복 실패하여 재계획으로 생성됨 "
                f"-- {phase_dict.get('replan_reason', '')}"
                if phase_dict.get("replanned_from")
                else ""
            ),
            human_escalated=bool(phase_dict.get("human_escalated")),
            human_feedback=str(phase_dict.get("human_escalation_choice", "")),
        )
        self.planner.record_phase_report(record)

    def _build_final_files_summary(self) -> dict[str, str]:
        summary: dict[str, str] = {}
        for name in APP_FILES + ELECTRON_FILES:
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
