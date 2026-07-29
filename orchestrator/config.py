"""Central place for pipeline configuration, loaded from environment/.env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
TARGET_APP_DIR = ROOT_DIR / "target-app"
STATE_DIR = ROOT_DIR / "state"
LOGS_DIR = ROOT_DIR / "logs"
CONFIG_DIR = ROOT_DIR / "config"

load_dotenv(ROOT_DIR / ".env", encoding="utf-8")


@dataclass(frozen=True)
class PipelineConfig:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")

    planner_model: str = os.getenv("PLANNER_MODEL", "gpt-4.1")
    developer_model: str = os.getenv("DEVELOPER_MODEL", "gpt-4.1")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "gpt-4.1")
    gui_tester_model: str = os.getenv("GUI_TESTER_MODEL", "gpt-4.1")

    # Single switch for which API *every* agent talks to (agents/llm_client.py)
    # -- "openai" (default) / "anthropic" / "gemini" / "ollama". The *_MODEL
    # settings above are still per-agent (different agents can reasonably
    # want different model sizes), but they're all interpreted against this
    # one provider -- e.g. with LLM_PROVIDER=anthropic, PLANNER_MODEL might
    # be "claude-opus-5" and GUI_TESTER_MODEL "claude-sonnet-5" (needs
    # vision + tool use). Changing providers is this one setting, not four.
    llm_provider: str = os.getenv("LLM_PROVIDER", "openai").lower()
    anthropic_api_key: str | None = os.getenv("ANTHROPIC_API_KEY")
    gemini_api_key: str | None = os.getenv("GEMINI_API_KEY")
    ollama_base_url: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

    # Retry upper bounds referenced in the design doc's workflow (section 3/6).
    max_dev_review_retries: int = int(os.getenv("MAX_DEV_REVIEW_RETRIES", "5"))
    max_gui_retries: int = int(os.getenv("MAX_GUI_RETRIES", "5"))
    max_replan_attempts: int = int(os.getenv("MAX_REPLAN_ATTEMPTS", "2"))
    # Developer<->GUI Tester rewrite loop (orchestrator/plan_pipeline.py),
    # tracked separately from the Developer<->Reviewer loop above via
    # plan.json's per-phase gui_retry_count field.
    max_gui_test_retries: int = int(os.getenv("MAX_GUI_TEST_RETRIES", "4"))

    # Debug-only: skip Reviewer for one phase and inject a deliberate bug
    # into the Developer's output before GUI verification, to demo the
    # GUI-failure -> Developer-rewrite loop. See debug/bug_injector.py.
    #
    # DEBUG_INJECT_PHASE_ID accepts a comma-separated list of candidate
    # phase ids (e.g. "phase-1,phase-2") -- which phase actually implements
    # the add-Todo flow varies by run since the Planner splits Phases
    # differently each time, so the *first* one of these that's actually
    # processed with matching code gets the bug (see plan_pipeline.py's
    # _apply_debug_bug()); the rest are just untouched candidates, not a
    # multi-bug mode.
    debug_inject_bug: bool = os.getenv("DEBUG_INJECT_BUG", "false").lower() == "true"
    debug_inject_phase_ids: frozenset[str] = frozenset(
        p.strip() for p in os.getenv("DEBUG_INJECT_PHASE_ID", "").split(",") if p.strip()
    )

    target_app_dir: Path = TARGET_APP_DIR
    state_dir: Path = STATE_DIR
    logs_dir: Path = LOGS_DIR
    state_file: Path = STATE_DIR / "pipeline_state.json"
    plan_file: Path = STATE_DIR / "plan.json"
    screenshots_dir: Path = LOGS_DIR / "screenshots"
    requirement_file: Path = CONFIG_DIR / "requirement.txt"
