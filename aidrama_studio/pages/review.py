from aidrama_studio.components.page_header import page_header
from aidrama_studio.pages._shared import coming_soon


def render() -> None:
    page_header("QC & Review", "QUALITY CONTROL", "以技术检查和人工门禁保障交付质量。")
    coming_soon(
        "QC 与审核门禁将在后续 Task 开启",
        "当前项目还没有可审核的制作产物。",
        ["自动 QC 报告", "风险与缺陷列表", "通过 / 退回 Review Gate"],
    )
