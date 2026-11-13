"""通用工具：日志、文本规范化、去重键、金额/日期解析。"""
from __future__ import annotations

import hashlib
import logging
import re
import sys
from datetime import datetime
from pathlib import Path

_LOGGER_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _LOGGER_CONFIGURED
    if not _LOGGER_CONFIGURED:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%H:%M:%S",
        ))
        root = logging.getLogger("bid_system")
        root.setLevel(logging.INFO)
        root.handlers.clear()
        root.addHandler(handler)
        root.propagate = False
        _LOGGER_CONFIGURED = True
    return logging.getLogger(f"bid_system.{name}")


# ----------------------------------------------------------------- 文本规范化
_PUNCT_RE = re.compile(r"[\s　\-—_（）()【】\[\]《》<>、，,。.；;：:！!？?\"'“”‘’/\\|]+")


def normalize_title(title: str) -> str:
    """标题归一化：去空白标点、繁简混排的符号差异，用于跨平台转载去重。"""
    if not title:
        return ""
    t = title.strip().lower()
    # 去掉常见平台前后缀噪声
    for noise in ("【更正公告】", "更正公告", "招标公告", "中标公告", "竞争性磋商公告"):
        t = t.replace(noise.lower(), "")
    return _PUNCT_RE.sub("", t)


def normalize_code(code: str | None) -> str:
    if not code:
        return ""
    return re.sub(r"[\s\-_/]", "", code.strip().upper())


def project_key(project_code: str | None, region: str | None, title: str) -> str:
    """项目去重键：有编号用编号；没编号用 地区+归一化标题 的指纹。"""
    code = normalize_code(project_code)
    if code:
        return f"code:{code}"
    raw = f"{(region or '').strip()}|{normalize_title(title)}"
    return "fp:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:20]


def content_hash(*parts: object) -> str:
    blob = "||".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ----------------------------------------------------------------- 金额解析
_AMOUNT_RE = re.compile(
    r"(?:预算金额|预算|采购预算|项目预算|最高限价|控制价|金额)"
    r"[^\d]{0,12}?"
    r"([0-9]+(?:[,，][0-9]{3})*(?:\.[0-9]+)?\s*(?:万?元|万元|元))"
)
_NUMBER_RE = re.compile(r"[0-9]+(?:\.[0-9]+)?")


def parse_budget(text: str) -> float | None:
    """从中文公告正文抽取预算金额，统一换算为元。"""
    if not text:
        return None
    m = _AMOUNT_RE.search(text)
    if not m:
        return None
    raw = m.group(1).replace("，", "").replace(",", "")
    num_m = _NUMBER_RE.search(raw)
    if not num_m:
        return None
    value = float(num_m.group(0))
    if "万元" in raw or raw.strip().endswith("万元"):
        value *= 10000
    return round(value, 2)


# ----------------------------------------------------------------- 日期解析
_DATE_PATTERNS = [
    re.compile(r"(20\d{2})\s*[年\-/]\s*(\d{1,2})\s*[月\-/]\s*(\d{1,2})\s*日?"
               r"(?:[^0-9]{0,6}(\d{1,2})\s*[：:点]\s*(\d{1,2}))?"),
]


def parse_deadline(text: str) -> str | None:
    """抽取投标/响应截止时间，规范成 'YYYY-MM-DD HH:MM'。"""
    if not text:
        return None
    keyword_idx = -1
    for kw in ("截止", "递交", "开标"):
        i = text.find(kw)
        if i >= 0:
            keyword_idx = i
            break
    search_from = text[max(0, keyword_idx - 40): keyword_idx + 80] if keyword_idx >= 0 else text
    for pat in _DATE_PATTERNS:
        m = pat.search(search_from)
        if m:
            y, mo, d = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            hh = int(m.group(4)) if m.group(4) else 0
            mm = int(m.group(5)) if m.group(5) else 0
            try:
                dt = datetime(y, mo, d, hh, mm)
            except ValueError:
                return None
            return dt.strftime("%Y-%m-%d %H:%M")
    return None


def parse_published(value: str | None) -> str | None:
    """平台时间字符串统一为 ISO 本地格式，失败原样返回。"""
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(value.strip(), fmt).isoformat(timespec="minutes")
        except ValueError:
            continue
    return value.strip()


def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p
