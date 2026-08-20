from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header("制作中心", "PRODUCTION", "监控媒体任务、进度、日志与可恢复错误。")
    coming_soon(
        "制作任务将在后续 Task 接入",
        "MoneyPrinterTurbo 媒体内核保持可用，但 Task001 不提交生成任务。",
        ["Production Task 队列", "TTS / 字幕 / 素材", "渲染进度与重试"],
    )
