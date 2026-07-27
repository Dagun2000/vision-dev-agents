"""JSON-backed state tracker for the pipeline's progress.

Persists the current plan (Phases) plus a history of replans and human
escalations, so a run can be inspected or resumed from disk.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agents.models import Phase, PhaseStatus


class StateTracker:
    def __init__(self, state_file: Path) -> None:
        self.state_file = state_file
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.data: dict[str, Any] = {
            "requirement": None,
            "phases": [],
            "replan_history": [],
            "escalation_history": [],
            "updated_at": None,
        }

    def load(self) -> None:
        if self.state_file.exists():
            self.data = json.loads(self.state_file.read_text(encoding="utf-8"))

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        self.state_file.write_text(
            json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    def set_requirement(self, requirement: str) -> None:
        self.data["requirement"] = requirement

    def set_phases(self, phases: list[Phase]) -> None:
        self.data["phases"] = [asdict(phase) for phase in phases]
        self.save()

    def update_phase_status(self, phase_id: str, status: PhaseStatus) -> None:
        for phase in self.data["phases"]:
            if phase["id"] == phase_id:
                phase["status"] = status.value
                break
        self.save()

    def record_replan(self, phase_id: str, reason: str) -> None:
        self.data["replan_history"].append(
            {
                "phase_id": phase_id,
                "reason": reason,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.save()

    def record_escalation(self, phase_id: str, reason: str, human_feedback: str) -> None:
        self.data["escalation_history"].append(
            {
                "phase_id": phase_id,
                "reason": reason,
                "human_feedback": human_feedback,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        self.save()
