"""Manual smoke test for the Reviewer agent.

Part A (isolated, no plan.json involved): feeds the Reviewer a deliberately
broken app.js (add works, but the empty-input guard is missing) against a
success_criteria that requires it, and checks the Reviewer rejects it with
issues that call that out. Then feeds a fixed version of the same code and
checks it's approved. The DevResult.summary passed in Part A deliberately
*lies* ("empty-input guard already implemented") to prove the Reviewer
judges from code only, not the Developer's own account of what it did.

Part B (plan.json-driven): reviews the first real 'dev_done' Phase from
state/plan.json via review_next_dev_done_phase(), which also drives the
review<->rewrite loop with the Developer agent, and updates plan.json.

target-app/ is backed up before Part A and restored afterward so Part A's
synthetic fixtures don't clobber real accumulated progress.

Requires a real OPENAI_API_KEY in .env, and state/plan.json / target-app
to already have at least one Phase in "dev_done" (see
scripts/smoke_test_developer.py) for Part B.

Usage:
    uv run python scripts/smoke_test_reviewer.py
"""

from __future__ import annotations

from agents.developer import OpenAIDeveloperAgent
from agents.models import DevResult, Phase, PhaseStatus
from agents.reviewer import OpenAIReviewerAgent
from orchestrator.config import PipelineConfig
from orchestrator.encoding import ensure_utf8_stdio

TEST_PHASE = Phase(
    id="test-empty-input",
    title="Todo 추가 및 빈 입력 방지",
    description="입력창의 텍스트로 Todo를 추가하되, 빈 입력은 막는다.",
    success_criteria=[
        "입력창에 텍스트를 입력하고 추가 버튼을 클릭하면 목록에 새 항목이 표시되어야 한다.",
        "입력창이 비어 있거나 공백만 입력된 상태에서 추가 버튼을 클릭해도 새 항목이 생성되지 않아야 한다.",
    ],
    status=PhaseStatus.DEV_DONE,
)

BROKEN_INDEX_HTML = """\
<!DOCTYPE html>
<html lang="ko">
<head><meta charset="UTF-8"><title>Todo</title><link rel="stylesheet" href="style.css"></head>
<body>
  <form id="todo-form">
    <input id="todo-input" type="text">
    <button type="submit">추가</button>
  </form>
  <ul id="todo-list"></ul>
  <script src="app.js"></script>
</body>
</html>
"""

STYLE_CSS = "body { font-family: sans-serif; }\n"

# Deliberately missing the empty-input guard: whatever's in the input,
# trimmed or not, gets added as-is.
BROKEN_APP_JS = """\
(function () {
  'use strict';
  var todos = [];
  var todoForm = document.getElementById('todo-form');
  var todoInput = document.getElementById('todo-input');
  var todoList = document.getElementById('todo-list');

  function renderTodos() {
    todoList.innerHTML = '';
    todos.forEach(function (todo) {
      var item = document.createElement('li');
      item.textContent = todo.text;
      todoList.appendChild(item);
    });
  }

  function addTodo(text) {
    todos.push({ id: Date.now(), text: text });
    renderTodos();
  }

  todoForm.addEventListener('submit', function (event) {
    event.preventDefault();
    addTodo(todoInput.value);
    todoInput.value = '';
  });
}());
"""

FIXED_APP_JS = """\
(function () {
  'use strict';
  var todos = [];
  var todoForm = document.getElementById('todo-form');
  var todoInput = document.getElementById('todo-input');
  var todoList = document.getElementById('todo-list');

  function renderTodos() {
    todoList.innerHTML = '';
    todos.forEach(function (todo) {
      var item = document.createElement('li');
      item.textContent = todo.text;
      todoList.appendChild(item);
    });
  }

  function addTodo(text) {
    todos.push({ id: Date.now(), text: text });
    renderTodos();
  }

  todoForm.addEventListener('submit', function (event) {
    event.preventDefault();
    var text = todoInput.value.trim();
    if (!text) {
      return;
    }
    addTodo(text);
    todoInput.value = '';
  });
}());
"""


def _write_app_files(config: PipelineConfig, index_html: str, style_css: str, app_js: str) -> None:
    config.target_app_dir.mkdir(parents=True, exist_ok=True)
    (config.target_app_dir / "index.html").write_text(index_html, encoding="utf-8")
    (config.target_app_dir / "style.css").write_text(style_css, encoding="utf-8")
    (config.target_app_dir / "app.js").write_text(app_js, encoding="utf-8")


def _backup_app_files(config: PipelineConfig) -> dict[str, str | None]:
    backup: dict[str, str | None] = {}
    for name in ("index.html", "style.css", "app.js"):
        path = config.target_app_dir / name
        backup[name] = path.read_text(encoding="utf-8") if path.exists() else None
    return backup


def _restore_app_files(config: PipelineConfig, backup: dict[str, str | None]) -> None:
    for name, content in backup.items():
        path = config.target_app_dir / name
        if content is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(content, encoding="utf-8")


def run_part_a(config: PipelineConfig, reviewer: OpenAIReviewerAgent) -> None:
    print("=" * 70)
    print("Part A-1: 빈 입력 방지 로직이 빠진 코드 -> 리뷰 (rejected 기대)")
    print("=" * 70)
    _write_app_files(config, BROKEN_INDEX_HTML, STYLE_CSS, BROKEN_APP_JS)
    # This summary is a lie on purpose -- the Reviewer must not trust it.
    lying_dev_result = DevResult(
        phase_id=TEST_PHASE.id,
        summary="빈 입력 방지 로직이 이미 구현되어 있습니다.",
        files_changed=["index.html", "style.css", "app.js"],
    )
    result = reviewer.review(TEST_PHASE, lying_dev_result)
    print(f"approved={result.passed}")
    for issue in result.issues:
        print(f"  - {issue}")
    if result.passed:
        print("!! 예상과 다름: 빈 입력 방지가 빠졌는데 승인되었습니다.")
    else:
        print("-> 기대대로 rejected, DevResult.summary의 거짓 주장에 속지 않음.")

    print()
    print("=" * 70)
    print("Part A-2: 빈 입력 방지 로직을 추가한 코드 -> 재리뷰 (approved 기대)")
    print("=" * 70)
    _write_app_files(config, BROKEN_INDEX_HTML, STYLE_CSS, FIXED_APP_JS)
    fixed_dev_result = DevResult(
        phase_id=TEST_PHASE.id,
        summary="빈 입력 방지 로직을 추가했습니다.",
        files_changed=["index.html", "style.css", "app.js"],
    )
    result = reviewer.review(TEST_PHASE, fixed_dev_result)
    print(f"approved={result.passed}")
    for issue in result.issues:
        print(f"  - {issue}")
    if not result.passed:
        print("!! 예상과 다름: 정상 코드인데 rejected되었습니다.")
    else:
        print("-> 기대대로 approved.")
    print()


def run_part_b(config: PipelineConfig, reviewer: OpenAIReviewerAgent) -> None:
    print("=" * 70)
    print("Part B: plan.json 기반 review_next_dev_done_phase() 통합 테스트")
    print("=" * 70)

    import json

    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    if not any(p["status"] == PhaseStatus.DEV_DONE.value for p in plan["phases"]):
        print("plan.json에 dev_done 상태인 Phase가 없어 Developer로 하나 만듭니다...")
        OpenAIDeveloperAgent(config=config).develop_next_pending_phase()

    result = reviewer.review_next_dev_done_phase()
    print(f"phase_id={result.phase_id} approved={result.passed}")
    for issue in result.issues:
        print(f"  - {issue}")

    plan = json.loads(config.plan_file.read_text(encoding="utf-8"))
    updated = next(p for p in plan["phases"] if p["id"] == result.phase_id)
    print(f"plan.json 상의 최종 status: {updated['status']}")


def main() -> None:
    ensure_utf8_stdio()
    config = PipelineConfig()
    reviewer = OpenAIReviewerAgent(config=config)

    backup = _backup_app_files(config)
    try:
        run_part_a(config, reviewer)
    finally:
        _restore_app_files(config, backup)

    run_part_b(config, reviewer)


if __name__ == "__main__":
    main()
