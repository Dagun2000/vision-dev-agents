"""Stub implementation of the Reviewer agent.

TODO (next step): call the LLM to semantically review dev_result against
phase.success_criteria (logic errors, requirement mismatches), independent
of the Developer's own context.
"""

from __future__ import annotations

from agents.base import ReviewerAgent
from agents.models import DevResult, Phase, ReviewResult


class StubReviewerAgent(ReviewerAgent):
    def review(self, phase: Phase, dev_result: DevResult) -> ReviewResult:
        raise NotImplementedError("StubReviewerAgent.review is not implemented yet")
