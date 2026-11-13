"""mock 平台侧：用 reportlab 生成中文招标公告 PDF（内置 STSong-Light，无需外部字体）。"""
from __future__ import annotations

import io

from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

_FONT = "STSong-Light"
pdfmetrics.registerFont(UnicodeCIDFont(_FONT))


def _fmt_money(v: float) -> str:
    s = f"{v:,.2f}"
    # 大额用万元表述，更贴近真实公告写法
    if v >= 10000:
        return f"人民币{s}元（{v / 10000:.2f}万元）"
    return f"人民币{s}元"


def build_notice_pdf(*, title: str, project_code: str, region: str, purchaser: str,
                     budget: float, deadline: str, content: str = "") -> bytes:
    """生成一份结构仿真的公告 PDF，采集端需自行抽取预算与截止时间。"""
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    y = height - 25 * mm

    def line(text: str, size: int = 11, gap: float = 9 * mm, bold_gap: bool = False) -> None:
        nonlocal y
        c.setFont(_FONT, size + (2 if bold_gap else 0))
        # 简单折行：每行约 38 个汉字
        while text:
            seg, text = text[:38], text[38:]
            c.drawString(22 * mm, y, seg)
            y -= gap
            if y < 25 * mm:
                c.showPage()
                y = height - 25 * mm
                c.setFont(_FONT, size)

    line(title, size=16, gap=14 * mm, bold_gap=True)
    line(f"项目编号：{project_code}")
    line(f"所在地区：{region}")
    line(f"采购人：{purchaser}")
    line("")
    line("一、项目基本情况")
    line(f"预算金额：{_fmt_money(budget)}")
    line(f"投标截止时间（开标时间）：{deadline}")
    line("")
    line("二、其他补充事宜")
    line(content or "申请人请于截止时间前通过平台递交加密电子投标文件，逾期不予受理。")
    line("")
    line("三、联系方式")
    line(f"采购人联系方式：{purchaser} 综合科 0571-8888XXXX")
    c.save()
    return buf.getvalue()
