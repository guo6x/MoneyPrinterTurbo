from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header("分镜导演台", "SHOT DIRECTOR", "把剧本拆解为可执行、可审核的镜头列表。")
    coming_soon(
        "分镜导演台将在后续 Task 开启",
        "Task001 不生成 Shot List，也不会调用 AI 工作流。",
        ["镜头排序与时长", "风险等级", "镜头与资产绑定"],
    )
