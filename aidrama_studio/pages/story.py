from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header(
        "创意与剧本", "STORY DEVELOPMENT", "从一句创意建立可生产的短剧叙事蓝图。"
    )
    coming_soon(
        "故事开发将在后续 Task 开启",
        "Task001 已建立项目上下文；Story Bible 与结构化剧本尚未生成。",
        ["Story Bible", "角色关系与世界观", "结构化场景和剧情节拍"],
    )
