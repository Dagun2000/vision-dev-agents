"""Stub implementation of the Developer agent.

TODO (next step): call the LLM to write/edit files under target-app/,
run an embedded linter/AST checker to self-correct syntax issues, and
start a local static server to serve the app for GUI verification.
"""

from __future__ import annotations

from agents.base import DeveloperAgent
from agents.models import DevResult, Phase


class StubDeveloperAgent(DeveloperAgent):
    def implement(self, phase: Phase, feedback: str | None = None) -> DevResult:
        raise NotImplementedError("StubDeveloperAgent.implement is not implemented yet")
