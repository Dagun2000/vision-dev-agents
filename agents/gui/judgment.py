"""Vision-based judgment for the GUI Tester agent.

Pure judgment: given a labeled (Set-of-marks) screenshot and the current
Phase's success_criteria, ask a vision-capable model to read the screenshot
itself -- no separate OCR text list is provided -- and decide the next
action, or declare success/failure. This module must never touch the OS, a
browser, or the DOM; see agents/gui/execution.py for that. It only knows
images and the model.

The actual provider call goes through agents/llm_client.py (shared by all
four agents, picked by the single LLM_PROVIDER setting) -- this module just
builds the prompt/image parts and asks for GUIActionSchema back.
"""

from __future__ import annotations

import io
import logging

from PIL import Image

from agents.llm_client import ImagePart, TextPart, structured_completion
from agents.schemas import GUIActionSchema
from orchestrator.config import PipelineConfig

logger = logging.getLogger("pipeline")

SYSTEM_PROMPT = """\
당신은 멀티 에이전트 자동 개발 파이프라인의 GUI 검증(GUI Tester) 에이전트입니다.
전달되는 스크린샷 위에는 클릭 가능해 보이는 요소마다 빨간 배지에 번호가
매겨져 있습니다.

규칙:
- 이미지 안의 텍스트와 레이아웃을 직접 읽고, 각 번호가 화면의 어떤 요소인지
  스스로 판단하세요. 번호별 설명은 별도로 제공되지 않습니다.
- **매우 중요**: 배지 번호는 스크린샷마다 처음부터 다시 계산됩니다. 화면
  구성이 조금이라도 달라지면(예: 카드가 다른 컬럼으로 이동해서 그 컬럼이
  비었다/찼다가 바뀌는 경우) 번호와 실제 요소의 대응이 이전 스텝과 완전히
  달라질 수 있습니다. "이전 스텝에서 5번이 '완료' 컬럼이었으니 지금도
  5번은 '완료' 컬럼이다"처럼 이전 스텝의 번호 의미를 그대로 재사용하지
  마세요. 매 스텝마다 지금 이 스크린샷만 보고 번호가 실제로 어떤 요소인지
  (컬럼 제목 텍스트 등을 직접 읽어서) 새로 판단하세요. 특히 드래그 이동
  결과를 확인할 때는 "몇 번으로 드래그했는지"가 아니라 지금 화면에서 카드가
  놓인 컬럼의 제목 텍스트를 직접 읽어서 어느 컬럼으로 이동했는지 판단하세요.
- 주어진 성공 조건(success_criteria)을 만족시키기 위해 다음에 수행할 단 하나의
  액션만 결정하세요: 어떤 번호를 클릭할지, 어떤 번호에 어떤 텍스트를
  입력할지, 또는 어떤 번호를 어떤 번호 위로 드래그해서 놓을지.
- 드래그 앤 드롭(예: 카드를 다른 목록/컬럼으로 옮기기)을 검증해야 한다면
  action="drag"를 사용하세요. target_element에는 옮길 대상(드래그를
  시작할 요소)의 번호를, drop_target_element에는 그것을 놓을 목적지
  요소의 번호를 넣으세요. 목적지가 비어 있는 컬럼처럼 번호가 매겨진
  요소가 화면에 전혀 없다면, 그 컬럼 안에 이미 있는 다른 카드나 컬럼
  제목처럼 번호가 매겨진 가장 가까운 요소를 목적지로 사용하세요.
- 지금 화면과 "지금까지 수행한 액션" 기록을 함께 근거로 판단하세요. 성공
  조건 중 일부는 지금 이 순간의 화면만으로는 확인할 수 없고, 몇 스텝 전에
  일으킨 변화(예: 로그아웃 버튼을 눌렀더니 로그인 화면으로 돌아간 것)를
  기록에서 근거로 삼아야만 판단할 수 있습니다. 모든 성공 조건을 각각 한
  번이라도 직접 관찰해서 확인했다면 action="success"를 선택하세요.
- **매우 중요**: 성공 조건은 "그 동작을 시도했다"가 아니라 "그 동작이
  실제로 화면에서 기대한 결과를 일으키는 것을 직접 확인했다"를 의미합니다.
  예를 들어 드래그나 클릭을 몇 번 실행했다는 기록 자체는 "이동/변경된다"는
  조건을 절대 만족시키지 않습니다 -- 실제로 그 결과(카드가 다른 컬럼에
  표시됨 등)가 화면에 나타나는 것을 직접 봐야만 그 조건이 충족된
  것입니다. **만약 어떤 성공 조건을 시도했는데 화면에서 그 조건이
  충족되지 않는 것을 직접 관찰했다면(예: 여러 번 드래그했는데도 카드가
  원래 컬럼에 그대로 남아 있음), 다른 조건들을 전부 확인했더라도 절대
  action="success"를 선택하지 마세요.** 이 경우 action="failure"를
  선택하고 criterion_failed에 정확히 그 실패한 조건을 적으세요. "시도는
  했으니 넘어가자"는 식으로 관찰된 실패를 무시하고 다른 조건 확인으로
  넘어가서 전체를 성공 처리하면 안 됩니다.
- **이미 한 번 확인한 성공 조건을 검증하려고 같은 절차(예: 로그인 ->
  로그아웃 -> 재로그인 -> 로그아웃 -> ...)를 여러 번 반복하지 마세요.**
  한 조건당 한 번만 확인하면 충분합니다. 스텝 수는 한정되어 있으므로,
  이미 확인한 조건을 또 검증하는 대신 아직 확인하지 않은 다른 성공 조건이
  있는지 먼저 확인하고, 모두 확인했다면 즉시 action="success"를 선택하세요.
- **매우 중요**: 어떤 성공 조건을 이미 직접 관찰해서 확인했다면(예: "이번
  Phase의 첫 화면에서 로그인 상태가 이미 복원되어 있었다"), 그 이후에
  다른 조건을 확인하려고 수행한 액션(예: 로그아웃 버튼을 눌러 로그아웃
  기능 자체를 테스트) 때문에 화면이 바뀌어 지금은 그 조건이 더 이상 눈에
  보이지 않더라도, 이미 확인했던 조건이 다시 충족되지 않은 것으로 되돌아간
  것이 아닙니다. "지금 이 순간의 화면"만 보고 이미 확인을 마친 조건을
  다시 판단하지 마세요 -- "지금까지 수행한 액션" 기록에 그 조건을 만족하는
  관찰이 이미 있는지 먼저 확인하고, 있다면 그 조건은 여전히 충족된
  것입니다. 예를 들어 "Phase 시작 직후 화면에 X가 보여야 한다"는 조건은
  오직 그 Phase의 *첫* 화면 하나만으로 판단하는 것이지, 이후 몇 스텝이
  지난 지금 화면에 X가 보이는지로 다시 판단하는 것이 아닙니다.
- **매우 중요**: "지금까지 수행한 액션" 기록에는 다른 Phase에서 있었던
  액션도 `[phase-N]` 태그와 함께 섞여 있을 수 있습니다. **지금 검증해야
  할 Phase(아래 "현재 Phase"에 표시됨)와 다른 태그가 붙은 기록은 그
  Phase의 성공 조건이 충족되었다는 증거로 절대 사용하지 마세요** --
  그 기록은 단지 "이 계정이 이미 존재한다", "지금 로그인/로그아웃
  상태가 이렇다" 같은 배경 정보로만 참고하세요. 지금 이 Phase의
  success_criteria는 반드시 지금 이 Phase 안에서 직접 관찰(화면을 보거나
  액션을 실행)해서 확인해야 하며, 다른 Phase에서 있었던 일을 근거로
  action="success"를 선택하면 안 됩니다. 만약 현재 화면이 이번 Phase가
  검증해야 할 기능과 무관한 상태(예: 다른 Phase의 결과로 이미 로그인/
  로그아웃되어 있는 화면)라면, 필요한 상태로 먼저 이동한 뒤 이번 Phase의
  조건을 직접 확인하세요.
- 더 진행해도 성공 조건을 만족시킬 수 없다고 판단되면 (예: 필요한 요소가
  화면에 보이지 않음, 클릭해도 기대한 변화가 일어나지 않음) action="failure"를
  선택하세요. 이때 criterion_failed에 어떤 성공 조건이 충족되지 않았는지
  (주어진 성공 조건 중 하나를 그대로, 또는 가장 가까운 것을) 적으세요.
- reasoning에는 화면 또는 액션 기록에서 실제로 관찰한 내용을 근거로 판단
  이유를 적으세요.
"""


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


STEP_BUDGET_WARNING_RATIO = 0.6


def _build_user_text(
    phase_id: str,
    success_criteria: list[str],
    step_log: list[str] | None,
    current_step: int,
    max_steps: int,
) -> str:
    criteria_text = "\n".join(f"- {c}" for c in success_criteria)
    parts = [
        f"## 현재 Phase\n{phase_id}",
        f"\n## 이번 Phase 진행 상황\n지금은 이번 Phase의 {current_step}/{max_steps}번째 스텝입니다.",
        f"\n## 이 Phase의 성공 조건\n{criteria_text}",
    ]
    if current_step >= max_steps * STEP_BUDGET_WARNING_RATIO:
        parts.append(
            f"\n## 주의\n이번 Phase의 스텝 예산 {max_steps}회 중 이미 {current_step}회를 "
            "사용했습니다. 지금부터는 새로운 조건을 확인하는 액션만 하고, 이미 한 번이라도 "
            "확인한 절차(로그인/로그아웃 반복 등)는 다시 하지 마세요. 모든 조건을 각각 한 "
            "번씩 확인했다면 지금 바로 action=\"success\"를 선택하세요."
        )
    if step_log:
        history = "\n".join(f"{i + 1}. {entry}" for i, entry in enumerate(step_log))
        parts.append(
            f"\n## 지금까지 수행한 액션 (여러 Phase가 섞여 있을 수 있음 -- "
            f"[{phase_id}] 태그가 붙은 것만 이번 Phase의 직접 증거입니다)\n{history}"
        )
    else:
        parts.append("\n## 지금까지 수행한 액션\n(아직 없음 -- 이번이 첫 액션)")
    return "\n".join(parts)


def decide_next_action(
    config: PipelineConfig,
    labeled_screenshot: Image.Image,
    success_criteria: list[str],
    phase_id: str,
    current_step: int = 1,
    max_steps: int = 1,
    step_log: list[str] | None = None,
) -> GUIActionSchema:
    user_text = _build_user_text(phase_id, success_criteria, step_log, current_step, max_steps)
    parts = [TextPart(user_text), ImagePart(_encode_png(labeled_screenshot))]

    result = structured_completion(
        config=config,
        model=config.gui_tester_model,
        system_prompt=SYSTEM_PROMPT,
        parts=parts,
        schema=GUIActionSchema,
    )
    logger.info(
        "GUI: vision(%s/%s) decided action=%s target=%s text=%r reasoning=%s",
        config.llm_provider,
        config.gui_tester_model,
        result.action,
        result.target_element,
        result.text,
        result.reasoning,
    )
    return result
