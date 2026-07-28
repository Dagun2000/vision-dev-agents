"""Manual smoke test for the Developer agent's develop_next_pending_phase().

Requires state/plan.json to already exist (run scripts/smoke_test_planner.py
first) and a real OPENAI_API_KEY in .env.

Reads the first Phase with status == "pending" from state/plan.json,
generates/updates target-app/{index.html,style.css,app.js}, self-lints
app.js, and writes the resulting status ("dev_done" / "lint_failed") back
to state/plan.json.

Usage:
    uv run python scripts/smoke_test_developer.py
"""

from __future__ import annotations

from agents.developer import DeveloperLintError, OpenAIDeveloperAgent
from orchestrator.config import PipelineConfig
from orchestrator.encoding import ensure_utf8_stdio


def main() -> None:
    ensure_utf8_stdio()
    config = PipelineConfig()
    developer = OpenAIDeveloperAgent(config=config)

    try:
        dev_result = developer.develop_next_pending_phase()
    except DeveloperLintError as exc:
        print(f"\n린트 실패로 중단됨: {exc}\n")
        return

    print(f"\nPhase '{dev_result.phase_id}' 개발 완료")
    print(f"요약: {dev_result.summary}")
    print(f"변경된 파일: {', '.join(dev_result.files_changed)}\n")

    for name in ("index.html", "style.css", "app.js"):
        path = config.target_app_dir / name
        content = path.read_text(encoding="utf-8")
        print(f"===== {name} ({len(content)} chars) =====")
        print(content)
        print()


if __name__ == "__main__":
    main()
