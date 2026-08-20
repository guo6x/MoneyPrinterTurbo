from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header("后期与成片", "POSTPRODUCTION", "汇总粗剪、审核版本与最终 MP4 导出。")
    coming_soon(
        "后期与导出将在后续 Task 开启",
        "Task001 不执行视频渲染或桌面导出。",
        ["Rough Cut", "版本对比", "Export MP4 与交付清单"],
    )
