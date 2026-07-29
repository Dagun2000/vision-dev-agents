"""Deliberate bug injection, for demoing the GUI Tester's failure-detection
and Developer-rewrite feedback loop (orchestrator/plan_pipeline.py).

Only used when DEBUG_INJECT_BUG=true and the current phase matches
DEBUG_INJECT_PHASE_ID (see orchestrator/config.py). Each injector takes the
Developer's generated app.js and returns (possibly-broken content,
description).

Two flavors of bug, both visible on screen but in different ways:
- disable_add_on_submit: nothing happens at all -- catchable by the
  screenshot-diff check alone (no visual change where one was expected).
- add_uses_placeholder_text (default): something DOES visibly happen (a
  new item appears), just with the wrong content -- diffing can't catch
  this, only the Vision model actually reading the result against
  success_criteria can. A better stress test of the judgment step, not
  just the execution step.

Add more injectors here as separate functions (e.g. a future
"drop_local_storage_save" for the persistence phase) and register them in
BUG_INJECTORS; nothing else needs to change.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger("pipeline")


def disable_add_on_submit(app_js: str) -> tuple[str, str]:
    """Neutralize the Todo-add submit handler: the form still exists and
    reacts to submit (preventDefault still runs, so no page reload), but
    the item never actually gets added -- clicking "추가" visibly does
    nothing, which the screenshot-diff check is built to catch.

    The `return;` is inserted *after* the handler's own
    `<param>.preventDefault()` call (not right at the top of the
    function), so the form's default submit/reload behavior stays
    suppressed -- otherwise a real page reload would itself count as a
    "screen changed" and defeat the point of this bug.
    """
    handler_pattern = re.compile(
        r"addEventListener\(\s*['\"]submit['\"]\s*,\s*function\s*\(\s*(\w+)\s*\)\s*\{"
    )
    handler_match = handler_pattern.search(app_js)
    if not handler_match:
        return app_js, "submit 이벤트 핸들러를 찾지 못해 버그를 주입하지 못함 (코드 구조가 예상과 다름)"

    param_name = handler_match.group(1)
    prevent_default_pattern = re.compile(
        rf"{re.escape(param_name)}\s*\.\s*preventDefault\s*\(\s*\)\s*;?"
    )
    prevent_default_match = prevent_default_pattern.search(app_js, handler_match.end())

    insertion_point = (
        prevent_default_match.end() if prevent_default_match else handler_match.end()
    )
    injected = (
        app_js[:insertion_point] + "\n    return;" + app_js[insertion_point:]
    )
    description = "Todo 추가 submit 핸들러를 (preventDefault 이후) 즉시 return하도록 무력화 -- 추가 버튼을 눌러도 목록에 아무 변화 없음"
    return injected, description


def add_uses_placeholder_text(app_js: str) -> tuple[str, str]:
    """Make the Todo-add handler ignore what the user actually typed and
    use the input's placeholder (greyed-out hint) text instead. A new item
    DOES appear -- this isn't a "nothing happens" bug -- but its content
    is wrong, so only a Vision model actually reading the result (not a
    pixel-diff check) can catch it.

    Deliberately does *not* touch the value-read line itself (that would
    also break the empty-input check right after it, since it typically
    tests the same variable for emptiness -- feeding it the placeholder,
    which is never empty, would make empty-input submissions silently
    "succeed" too, an entirely different bug from the one intended here).
    Instead, the swap is inserted right before the array .push() call, so
    it only fires *after* the real empty check already passed on the real
    value: typing something still shows the wrong (placeholder) text, but
    submitting empty is still correctly blocked.

    Matches both `var text = todoInput.value.trim();` and a plain
    `text = todoInput.value.trim();` (declared earlier), since the
    Developer's generated code has used both styles.
    """
    handler_pattern = re.compile(
        r"addEventListener\(\s*['\"]submit['\"]\s*,\s*function\s*\(\s*\w+\s*\)\s*\{"
    )
    handler_match = handler_pattern.search(app_js)
    if not handler_match:
        return app_js, "submit 이벤트 핸들러를 찾지 못해 버그를 주입하지 못함 (코드 구조가 예상과 다름)"

    value_read_pattern = re.compile(
        r"(?:var|let|const)?\s*(\w+)\s*=\s*(\w+)\s*\.\s*value\s*(?:\.\s*trim\s*\(\s*\)\s*)?;"
    )
    value_match = value_read_pattern.search(app_js, handler_match.end())
    if not value_match:
        return app_js, "입력값을 읽는 코드를 찾지 못해 버그를 주입하지 못함 (코드 구조가 예상과 다름)"

    text_var = value_match.group(1)
    input_var = value_match.group(2)

    push_pattern = re.compile(r"\.push\s*\(")
    push_match = push_pattern.search(app_js, value_match.end())
    if not push_match:
        return app_js, "새 Todo를 배열에 추가하는 push() 호출을 찾지 못해 버그를 주입하지 못함 (코드 구조가 예상과 다름)"

    # Insert as a whole new line right before the push call's line, matching
    # its indentation, so it runs after whatever empty-check sits between
    # the value read and the push (i.e. only once we know something was
    # actually typed).
    line_start = app_js.rfind("\n", 0, push_match.start()) + 1
    content_start = line_start
    while content_start < len(app_js) and app_js[content_start] in " \t":
        content_start += 1
    indent = app_js[line_start:content_start]
    injected = (
        app_js[:line_start]
        + f"{indent}{text_var} = {input_var}.placeholder;\n"
        + app_js[line_start:]
    )
    description = (
        f"Todo 추가 직전(빈 입력 검증은 통과한 뒤)에 실제 입력값 대신 입력창의 "
        f"placeholder(안내 문구) 텍스트로 덮어쓰도록 변경 ({input_var}.value -> "
        f"{input_var}.placeholder) -- 빈 입력은 여전히 정상적으로 막히고, 뭔가 입력했을 "
        f"때만 목록에 엉뚱한 내용으로 추가됨"
    )
    return injected, description


BUG_INJECTORS = {
    "disable_add_on_submit": disable_add_on_submit,
    "add_uses_placeholder_text": add_uses_placeholder_text,
}
DEFAULT_INJECTOR = "add_uses_placeholder_text"


def inject_bug(phase_id: str, app_js: str, injector_name: str = DEFAULT_INJECTOR) -> str:
    injector = BUG_INJECTORS.get(injector_name)
    if injector is None:
        raise ValueError(f"Unknown bug injector: {injector_name!r} (known: {list(BUG_INJECTORS)})")

    injected_js, description = injector(app_js)
    logger.warning("[DEBUG] Phase %s에 의도적 버그 주입됨: %s", phase_id, description)
    return injected_js
