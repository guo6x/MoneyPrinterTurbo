from __future__ import annotations

import json
import re

from pydantic import ValidationError

from aidrama_studio.domain import StoryBible


class StoryBibleParseError(ValueError):
    def __init__(self, message: str, *, raw: str = ""):
        super().__init__(message)
        self.raw = raw


def strip_code_fence(raw: str) -> str:
    text = raw.strip()
    fenced = re.fullmatch(
        r"```(?:json)?\s*(.*?)\s*```", text, re.IGNORECASE | re.DOTALL
    )
    return fenced.group(1).strip() if fenced else text


def _extract_first_object(text: str) -> str:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            _, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return text[index : index + end]
    raise StoryBibleParseError("AI 返回中没有找到有效 JSON 对象", raw=text)


def parse_story_bible(raw: str) -> StoryBible:
    if not isinstance(raw, str) or not raw.strip():
        raise StoryBibleParseError("AI 返回为空", raw=raw or "")
    cleaned = strip_code_fence(raw)
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        try:
            payload = json.loads(_extract_first_object(cleaned))
        except (json.JSONDecodeError, StoryBibleParseError) as exc:
            if isinstance(exc, StoryBibleParseError):
                raise exc
            raise StoryBibleParseError("AI 返回不是有效 JSON", raw=cleaned) from exc
    if not isinstance(payload, dict):
        raise StoryBibleParseError("Story Bible JSON 顶层必须是对象", raw=cleaned)
    try:
        return StoryBible.model_validate(payload)
    except ValidationError as exc:
        raise StoryBibleParseError(
            "Story Bible 结构校验失败："
            + "; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ),
            raw=cleaned,
        ) from exc
