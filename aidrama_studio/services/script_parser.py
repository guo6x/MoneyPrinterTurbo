from __future__ import annotations
import json, re
from pydantic import ValidationError
from aidrama_studio.domain import StructuredScript

class StructuredScriptParseError(ValueError):
    def __init__(self, message: str, *, raw: str = ""):
        super().__init__(message); self.raw = raw

def parse_structured_script(raw: str) -> StructuredScript:
    if not isinstance(raw, str) or not raw.strip(): raise StructuredScriptParseError("AI 返回为空", raw=raw or "")
    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.I | re.S)
    text = fenced.group(1).strip() if fenced else text
    try: payload = json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for i, c in enumerate(text):
            if c == "{":
                try: payload, _ = decoder.raw_decode(text[i:]); break
                except json.JSONDecodeError: pass
        else: raise StructuredScriptParseError("AI 返回不是有效 JSON", raw=text)
    if not isinstance(payload, dict): raise StructuredScriptParseError("剧本 JSON 顶层必须是对象", raw=text)
    try: return StructuredScript.model_validate(payload)
    except ValidationError as exc:
        raise StructuredScriptParseError("剧本结构校验失败：" + "; ".join(f"{'.'.join(map(str,e['loc']))}: {e['msg']}" for e in exc.errors()), raw=text) from exc
