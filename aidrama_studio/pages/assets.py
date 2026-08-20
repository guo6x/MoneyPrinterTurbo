from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header("角色与场景", "ASSET LIBRARY", "管理角色、场景与视觉连续性资产。")
    coming_soon(
        "资产库将在后续 Task 开启",
        "当前阶段尚未建立角色、场景与 Asset Lock。",
        ["角色定妆资产", "场景视觉资产", "版本与锁定状态"],
    )
