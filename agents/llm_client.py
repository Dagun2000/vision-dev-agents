"""Shared multi-provider structured-output client, used by all four agents.

One env var (LLM_PROVIDER) controls every agent's provider at once --
Planner/Developer/Reviewer/GUI Tester all call structured_completion()
below instead of talking to an SDK directly, so switching providers never
means editing more than one place. Per-agent model names (PLANNER_MODEL,
DEVELOPER_MODEL, REVIEWER_MODEL, GUI_TESTER_MODEL) still exist separately --
different agents can reasonably want different model *sizes* within the
same provider.

Each provider's SDK is imported inside its own function, not at module
level, so the cost of importing all four is only paid once actually used.
"""

from __future__ import annotations

import base64
import logging
import time
from dataclasses import dataclass
from typing import TypeVar

import anthropic
import httpx
import openai
from pydantic import BaseModel

from orchestrator.config import PipelineConfig

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger("pipeline")

# A single dropped connection used to kill an entire multi-minute,
# multi-phase pipeline run outright -- confirmed real (openai's
# APIConnectionError renders as literally "Connection error.", an exact
# match for a run that had already passed two Phases before dying on this).
# Retried with backoff; NOT retried: anything that isn't transient (bad
# API key, invalid request, schema mismatch, etc.) -- retrying those would
# just burn time and money without ever succeeding.
RETRYABLE_EXCEPTIONS = (
    openai.APIConnectionError,
    openai.APITimeoutError,
    openai.InternalServerError,
    openai.RateLimitError,
    anthropic.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.InternalServerError,
    anthropic.RateLimitError,
    httpx.TransportError,  # covers ConnectError/ReadTimeout/etc. -- also
    # what gemini/ollama's own httpx-based clients raise for the same
    # class of transient network failure.
    ConnectionError,
    TimeoutError,
)
MAX_LLM_RETRIES = 3
RETRY_BACKOFF_SECONDS = 2.0


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    data: bytes
    mime_type: str = "image/png"


Part = TextPart | ImagePart


def structured_completion(
    config: PipelineConfig,
    model: str,
    system_prompt: str,
    parts: list[Part],
    schema: type[T],
) -> T:
    """Ask `model` (on config.llm_provider) for output matching `schema`,
    given a system prompt and a list of user-turn parts (text and/or
    images -- most agents only ever pass TextPart, GUI Tester's judgment
    call adds an ImagePart)."""
    provider = config.llm_provider
    dispatch = {
        "openai": _openai,
        "anthropic": _anthropic,
        "gemini": _gemini,
        "ollama": _ollama,
    }
    call = dispatch.get(provider)
    if call is None:
        raise ValueError(
            f"Unknown LLM_PROVIDER={provider!r} (expected one of: {', '.join(dispatch)})"
        )

    for attempt in range(1, MAX_LLM_RETRIES + 1):
        try:
            return call(config, model, system_prompt, parts, schema)
        except RETRYABLE_EXCEPTIONS as exc:
            if attempt == MAX_LLM_RETRIES:
                logger.error(
                    "LLM: %s call failed after %d attempts (%s) -- giving up",
                    provider,
                    MAX_LLM_RETRIES,
                    exc,
                )
                raise
            wait_seconds = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
            logger.warning(
                "LLM: %s call failed (attempt %d/%d): %s -- retrying in %.0fs",
                provider,
                attempt,
                MAX_LLM_RETRIES,
                exc,
                wait_seconds,
            )
            time.sleep(wait_seconds)
    raise AssertionError("unreachable")  # loop always returns or raises


def _openai(
    config: PipelineConfig, model: str, system_prompt: str, parts: list[Part], schema: type[T]
) -> T:
    from openai import OpenAI

    client = OpenAI(api_key=config.openai_api_key)
    content: list[dict] = []
    for part in parts:
        if isinstance(part, TextPart):
            content.append({"type": "input_text", "text": part.text})
        else:
            image_b64 = base64.b64encode(part.data).decode("ascii")
            content.append(
                {"type": "input_image", "image_url": f"data:{part.mime_type};base64,{image_b64}"}
            )

    response = client.responses.parse(
        model=model,
        input=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
        text_format=schema,
    )
    result = response.output_parsed
    if result is None:
        raise ValueError(f"{schema.__name__}: openai model returned an unparseable response")
    return result


def _anthropic(
    config: PipelineConfig, model: str, system_prompt: str, parts: list[Part], schema: type[T]
) -> T:
    import anthropic

    client = anthropic.Anthropic(api_key=config.anthropic_api_key)
    content: list[dict] = []
    for part in parts:
        if isinstance(part, TextPart):
            content.append({"type": "text", "text": part.text})
        else:
            image_b64 = base64.b64encode(part.data).decode("ascii")
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": part.mime_type, "data": image_b64},
                }
            )

    # Claude has no direct "parse into this Pydantic model" convenience the
    # way OpenAI's Responses API does -- a forced tool call is the standard
    # way to get output that reliably matches a JSON schema.
    tool_name = schema.__name__
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        tools=[
            {
                "name": tool_name,
                "description": f"Return the result as {tool_name}.",
                "input_schema": schema.model_json_schema(),
            }
        ],
        tool_choice={"type": "tool", "name": tool_name},
        messages=[{"role": "user", "content": content}],
    )
    tool_use = next((b for b in response.content if b.type == "tool_use"), None)
    if tool_use is None:
        raise ValueError(f"{schema.__name__}: anthropic model did not return a tool_use block")
    return schema.model_validate(tool_use.input)


def _gemini(
    config: PipelineConfig, model: str, system_prompt: str, parts: list[Part], schema: type[T]
) -> T:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=config.gemini_api_key)
    gemini_parts = []
    for part in parts:
        if isinstance(part, TextPart):
            gemini_parts.append(types.Part.from_text(text=part.text))
        else:
            gemini_parts.append(types.Part.from_bytes(data=part.data, mime_type=part.mime_type))

    response = client.models.generate_content(
        model=model,
        contents=[types.Content(role="user", parts=gemini_parts)],
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            response_schema=schema,
        ),
    )
    if response.parsed is not None:
        return response.parsed
    return schema.model_validate_json(response.text)


def _ollama(
    config: PipelineConfig, model: str, system_prompt: str, parts: list[Part], schema: type[T]
) -> T:
    import ollama

    client = ollama.Client(host=config.ollama_base_url)
    text = "\n".join(part.text for part in parts if isinstance(part, TextPart))
    images = [
        base64.b64encode(part.data).decode("ascii") for part in parts if isinstance(part, ImagePart)
    ]
    message: dict = {"role": "user", "content": text}
    if images:
        message["images"] = images

    response = client.chat(
        model=model,
        messages=[{"role": "system", "content": system_prompt}, message],
        format=schema.model_json_schema(),
    )
    return schema.model_validate_json(response["message"]["content"])
