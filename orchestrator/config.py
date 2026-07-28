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

load_dotenv(ROOT_DIR / ".env", encoding="utf-8")


@dataclass(frozen=True)
class PipelineConfig:
    openai_api_key: str | None = os.getenv("OPENAI_API_KEY")

    planner_model: str = os.getenv("PLANNER_MODEL", "gpt-4.1")
    developer_model: str = os.getenv("DEVELOPER_MODEL", "gpt-4.1")
    reviewer_model: str = os.getenv("REVIEWER_MODEL", "gpt-4.1")
    gui_tester_model: str = os.getenv("GUI_TESTER_MODEL", "gpt-4.1")

    # Retry upper bounds referenced in the design doc's workflow (section 3/6).
    max_dev_review_retries: int = int(os.getenv("MAX_DEV_REVIEW_RETRIES", "5"))
    max_gui_retries: int = int(os.getenv("MAX_GUI_RETRIES", "5"))
    max_replan_attempts: int = int(os.getenv("MAX_REPLAN_ATTEMPTS", "2"))

    target_app_dir: Path = TARGET_APP_DIR
    state_dir: Path = STATE_DIR
    logs_dir: Path = LOGS_DIR
    state_file: Path = STATE_DIR / "pipeline_state.json"
    plan_file: Path = STATE_DIR / "plan.json"
