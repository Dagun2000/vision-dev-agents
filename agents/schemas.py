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


class DevOutputSchema(BaseModel):
    """Full contents of the three static app files for one Developer attempt.

    The model always returns all three files in full (not a diff) so each
    Phase can accumulate on top of the previous Phase's output.
    """

    index_html: str = Field(description="index.html 전체 내용")
    style_css: str = Field(description="style.css 전체 내용")
    app_js: str = Field(description="app.js 전체 내용 (바닐라 JS, module 금지)")
    summary: str = Field(description="이번 Phase에서 구현/수정한 내용에 대한 한두 문장 요약")


class ReviewOutputSchema(BaseModel):
    """Reviewer's semantic judgement of whether code satisfies success_criteria."""

    approved: bool = Field(description="모든 success_criteria를 의미적으로 만족하면 true")
    issues: list[str] = Field(
        default_factory=list,
        description="approved가 false일 때, 어떤 success_criteria가 왜 충족되지 않는지 "
        "구체적으로 적은 목록. approved가 true면 빈 배열",
    )
