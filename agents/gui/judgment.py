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
- 주어진 성공 조건(success_criteria)을 만족시키기 위해 다음에 수행할 단 하나의
  액션만 결정하세요: 어떤 번호를 클릭할지, 또는 어떤 번호에 어떤 텍스트를
  입력할지.
- 화면 상태만으로 성공 조건이 이미 충족되었다고 명확히 판단되면
  action="success"를 선택하세요.
- 더 진행해도 성공 조건을 만족시킬 수 없다고 판단되면 (예: 필요한 요소가
  화면에 보이지 않음, 클릭해도 기대한 변화가 일어나지 않음) action="failure"를
  선택하세요. 이때 criterion_failed에 어떤 성공 조건이 충족되지 않았는지
  (주어진 성공 조건 중 하나를 그대로, 또는 가장 가까운 것을) 적으세요.
- reasoning에는 화면에서 실제로 관찰한 내용을 근거로 판단 이유를 적으세요.
"""


def _encode_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _build_user_text(success_criteria: list[str], step_log: list[str] | None) -> str:
    criteria_text = "\n".join(f"- {c}" for c in success_criteria)
    parts = [f"## 성공 조건\n{criteria_text}"]
    if step_log:
        history = "\n".join(f"{i + 1}. {entry}" for i, entry in enumerate(step_log))
        parts.append(f"\n## 지금까지 수행한 액션\n{history}")
    else:
        parts.append("\n## 지금까지 수행한 액션\n(아직 없음 -- 이번이 첫 액션)")
    return "\n".join(parts)


def decide_next_action(
    config: PipelineConfig,
    labeled_screenshot: Image.Image,
    success_criteria: list[str],
    step_log: list[str] | None = None,
) -> GUIActionSchema:
    user_text = _build_user_text(success_criteria, step_log)
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
