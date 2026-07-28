"""Pydantic schemas used for LLM structured-output parsing.

Kept separate from agents/models.py: these describe the exact JSON shape
requested from the model, while models.py holds the plain dataclasses used
internally across agents and the orchestrator.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PlanPhaseSchema(BaseModel):
    id: str = Field(description="Sequential id, e.g. 'phase-1', 'phase-2'")
    title: str
    description: str
    success_criteria: list[str] = Field(
        description="One or more concrete, screen-observable conditions used by the "
        "Reviewer/GUI Tester agents to judge pass/fail"
    )
    status: Literal["pending"] = "pending"


class PlanSchema(BaseModel):
    phases: list[PlanPhaseSchema]
