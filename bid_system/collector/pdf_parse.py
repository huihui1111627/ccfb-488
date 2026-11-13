"""附件 PDF 解析：文本抽取 + 预算/截止时间提取。

下载逻辑放在 fetchers.BaseFetcher 里走 HttpClient 的退避重试封装，
本模块只负责已落地 PDF 的解析。
"""
from __future__ import annotations

from pathlib import Path

from pypdf import PdfReader

from ..utils import get_logger, parse_budget, parse_deadline

log = get_logger("pdf")


def extract_pdf_fields(pdf_path: Path) -> dict:
    """从 PDF 抽取预算金额与投标截止时间。"""
    reader = PdfReader(str(pdf_path))
    text = "\n".join(page.extract_text() or "" for page in reader.pages)
    fields = {
        "budget": parse_budget(text),
        "deadline": parse_deadline(text),
        "text_len": len(text),
    }
    log.info("PDF解析 %s -> 预算=%s 截止=%s", pdf_path.name, fields["budget"], fields["deadline"])
    return fields
