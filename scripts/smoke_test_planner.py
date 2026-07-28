"""Manual smoke test for the Planner agent's create_plan().

Requires a real OPENAI_API_KEY in .env. Prints the generated plan and
also writes it to state/plan.json (via OpenAIPlannerAgent itself).

Usage:
    uv run python scripts/smoke_test_planner.py
"""

from __future__ import annotations

import json

from agents.planner import OpenAIPlannerAgent
from orchestrator.encoding import ensure_utf8_stdio

SAMPLE_REQUIREMENT = (
    "로컬 저장이 되는 Todo 리스트 앱을 만들고 싶어. "
    "추가/완료체크/삭제 기능이 필요하고, 빈 입력은 막아야 해."
)


def main() -> None:
    ensure_utf8_stdio()
    planner = OpenAIPlannerAgent()
    phases = planner.create_plan(SAMPLE_REQUIREMENT)

    print(f"\n{len(phases)}개 Phase 생성됨:\n")
    print(
        json.dumps(
            [
                {
                    "id": p.id,
                    "title": p.title,
                    "description": p.description,
                    "success_criteria": p.success_criteria,
                    "status": p.status.value,
                }
                for p in phases
            ],
            indent=2,
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
