from __future__ import annotations
import json,re
from pydantic import ValidationError
from aidrama_studio.domain import ShotPlan
class ShotPlanParseError(ValueError):
    def __init__(self,message,*,raw=""): super().__init__(message); self.raw=raw
def parse_shot_plan(raw, *, expected_source_script_revision_id=None):
    if not isinstance(raw,str) or not raw.strip(): raise ShotPlanParseError("AI 返回为空",raw=raw or "")
    text=raw.strip(); m=re.fullmatch(r"```(?:json)?\s*(.*?)\s*```",text,re.I|re.S); text=m.group(1).strip() if m else text
    try: payload=json.loads(text)
    except json.JSONDecodeError:
        dec=json.JSONDecoder()
        for i,ch in enumerate(text):
            if ch=="{":
                try: payload,_=dec.raw_decode(text[i:]); break
                except json.JSONDecodeError: pass
        else: raise ShotPlanParseError("AI 返回不是有效 JSON",raw=text)
    expected_source_id = str(expected_source_script_revision_id or "").strip()
    if expected_source_id and isinstance(payload, dict):
        if "source_script_revision_id" not in payload:
            payload = dict(payload)
            payload["source_script_revision_id"] = expected_source_id
        elif payload["source_script_revision_id"] != expected_source_id:
            raise ShotPlanParseError(
                "Shot Plan source_script_revision_id conflicts with authoritative "
                f"product provenance: expected {expected_source_id!r}, got "
                f"{payload['source_script_revision_id']!r}",
                raw=text,
            )
    try: return ShotPlan.model_validate(payload)
    except ValidationError as e: raise ShotPlanParseError("Shot Plan 结构校验失败: "+str(e),raw=text) from e
