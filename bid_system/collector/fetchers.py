"""三个平台的采集适配器：列表分页 -> 详情 -> PDF 附件 -> 标准文档。

统一对外接口 fetch_platform(platform, mode, checkpoint) -> 生成器，
逐条产出标准 doc 字典，引擎负责落库、断点与重试。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Iterator

from bs4 import BeautifulSoup

from .. import config, db
from ..utils import get_logger, parse_published
from . import browser
from .http_client import AuthExpiredError, HttpClient
from .pdf_parse import extract_pdf_fields

log = get_logger("fetch")


@dataclass
class RawItem:
    platform_notice_id: str
    notice_type: str
    title: str
    region: str | None
    published_at: str | None
    detail_url: str


class BaseFetcher:
    kind = "base"

    def __init__(self, platform: dict) -> None:
        self.platform = platform
        self.code = platform["code"]
        self.name = platform["name"]
        self.base_url = platform["base_url"]
        self.http = HttpClient(self.base_url, self.code)

    def close(self) -> None:
        self.http.close()

    # 由子类实现
    def list_page(self, page: int) -> tuple[list[RawItem], int]:
        raise NotImplementedError

    def fetch_detail(self, item: RawItem) -> dict[str, Any]:
        raise NotImplementedError

    # ------------------------------------------------------------ 公共流程
    def iter_items(self, mode: str, checkpoint: db.sqlite3.Row | None,
                   ) -> Iterator[tuple[RawItem, dict]]:
        """翻列表到底（或增量水位为止），逐条进详情、解析 PDF，产出 (item, doc)。

        列表按发布时间升序分页，新公告出现在靠后的页。增量时只有「整页条目都在
        已见集合里」才允许提前停止，避免第一页是旧公告时误杀后面页里的新条目。
        """
        seen: set[str] = set()
        if checkpoint:
            seen = set(json.loads(checkpoint["seen_ids"] or "[]"))
        watermark = checkpoint["last_published"] if checkpoint else None

        page = 1
        total_pages = 1
        while page <= total_pages:
            items, total = self.list_page(page)
            total_pages = max(1, (total + 4) // 5)
            db.upsert_checkpoint(self.code, page, watermark, list(seen))
            if not items:
                break

            unseen = [it for it in items if it.platform_notice_id not in seen]
            if mode == "incremental" and not unseen:
                # 整页都是历史条目：再向后翻一页确认没有混入新条目，没有则结束
                next_items, _ = self.list_page(page + 1)
                if not next_items or all(
                        it.platform_notice_id in seen for it in next_items):
                    break

            for item in (unseen if mode == "incremental" else items):
                seen.add(item.platform_notice_id)
                doc = self.fetch_detail(item)
                yield item, doc
            page += 1

    def standard_doc(self, item: RawItem, detail: dict, pdf_path: str | None,
                     pdf_fields: dict | None) -> dict:
        return {
            "platform_code": self.code,
            "platform_name": self.name,
            "platform_notice_id": item.platform_notice_id,
            "notice_type": detail.get("type") or item.notice_type,
            "title": detail.get("title") or item.title,
            "url": f"{self.base_url}{item.detail_url}",
            "region": detail.get("region") or item.region,
            "budget": (pdf_fields or {}).get("budget"),
            "deadline": (pdf_fields or {}).get("deadline"),
            "purchaser": detail.get("purchaser"),
            "project_code": detail.get("project_code"),
            "pdf_path": pdf_path,
            "published_at": parse_published(item.published_at),
        }

    def _handle_pdf(self, item: RawItem, attachment_path: str) -> tuple[str | None, dict]:
        # 走 HttpClient 退避封装：429/5xx 自动重试，401 由 AuthFetcher 处理
        out_dir = config.PDF_DIR / self.code
        out_dir.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_." else "_"
                       for ch in str(item.platform_notice_id))
        dest = out_dir / f"{safe}.pdf"
        resp = self.http.request("GET", attachment_path, stream=True)
        try:
            with open(dest, "wb") as fh:
                for chunk in resp.iter_bytes():
                    fh.write(chunk)
        finally:
            resp.close()
        fields = extract_pdf_fields(dest)
        return str(dest), fields


# ===================================================================== HTML 平台
class HtmlFetcher(BaseFetcher):
    kind = "html"

    def list_page(self, page: int) -> tuple[list[RawItem], int]:
        resp = self.http.get("/notices", params={"page": page})
        soup = BeautifulSoup(resp.text, "html.parser")
        total = 0
        total_el = soup.select_one(".pager .total")
        if total_el and "共" in total_el.text:
            total = int("".join(ch for ch in total_el.text if ch.isdigit()) or 0)
        items: list[RawItem] = []
        for row in soup.select("tr.notice-row"):
            link = row.select_one("a.notice-link")
            tds = row.find_all("td")
            items.append(RawItem(
                platform_notice_id=tds[0].get_text(strip=True),
                notice_type=tds[2].get_text(strip=True),
                title=link.get_text(strip=True),
                region=tds[3].get_text(strip=True),
                published_at=tds[4].get_text(strip=True),
                detail_url=link["href"],
            ))
        return items, total

    def fetch_detail(self, item: RawItem) -> dict[str, Any]:
        resp = self.http.get(item.detail_url)
        soup = BeautifulSoup(resp.text, "html.parser")

        def meta(nid: str) -> str | None:
            el = soup.select_one(f"#{nid}")
            return el.get_text(strip=True) if el else None

        att = soup.select_one("#attachment")
        detail = {
            "title": (soup.select_one("#notice-title") or {}).get_text(strip=True)
            if soup.select_one("#notice-title") else item.title,
            "project_code": meta("project-code"),
            "type": meta("notice-type"),
            "region": meta("region"),
            "purchaser": meta("purchaser"),
        }
        pdf_path, fields = self._handle_pdf(item, att["href"])
        return {"detail": detail, "pdf_path": pdf_path, "pdf_fields": fields}

    def iter_items(self, mode, checkpoint):
        for item, payload in super().iter_items(mode, checkpoint):
            yield item, self.standard_doc(item, payload["detail"],
                                          payload["pdf_path"], payload["pdf_fields"])


# ===================================================================== JSON 平台
class JsonFetcher(BaseFetcher):
    kind = "json"

    def _get_json(self, url: str, **params) -> dict:
        resp = self.http.get(url, params=params)
        body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"{self.code} 接口返回异常: {body}")
        return body["data"]

    def list_page(self, page: int) -> tuple[list[RawItem], int]:
        data = self._get_json("/api/notices", page=page)
        items = [
            RawItem(
                platform_notice_id=n["id"], notice_type=n["type"],
                title=n["title"], region=n.get("region"),
                published_at=n.get("published_at"),
                detail_url=f"/api/notice/{n['id']}",
            ) for n in data["items"]
        ]
        return items, int(data["total"])

    def fetch_detail(self, item: RawItem) -> dict[str, Any]:
        detail = self._get_json(item.detail_url)
        pdf_path, fields = self._handle_pdf(item, detail["attachment_url"])
        return {"detail": detail, "pdf_path": pdf_path, "pdf_fields": fields}

    def iter_items(self, mode, checkpoint):
        for item, payload in super().iter_items(mode, checkpoint):
            yield item, self.standard_doc(item, payload["detail"],
                                          payload["pdf_path"], payload["pdf_fields"])


# ===================================================================== 登录平台
class AuthFetcher(JsonFetcher):
    kind = "auth"

    def __init__(self, platform: dict) -> None:
        super().__init__(platform)
        self._relogin()

    def _relogin(self) -> None:
        cookies = browser.login()
        self.http.close()
        self.http = HttpClient(self.base_url, self.code, cookies=cookies)
        log.info("[%s] 已建立登录会话", self.code)

    def _get_json(self, url: str, **params) -> dict:
        try:
            resp = self.http.get(url, params=params)
        except AuthExpiredError:
            log.warning("[%s] 会话过期，自动续登后重试", self.code)
            self._relogin()
            resp = self.http.get(url, params=params)
        body = resp.json()
        if body.get("code") == 401:
            log.warning("[%s] 接口报 401，自动续登后重试", self.code)
            self._relogin()
            resp = self.http.get(url, params=params)
            body = resp.json()
        if body.get("code") != 0:
            raise RuntimeError(f"{self.code} 接口返回异常: {body}")
        return body["data"]

    def _handle_pdf(self, item, attachment_path):
        try:
            return super()._handle_pdf(item, attachment_path)
        except AuthExpiredError:
            self._relogin()
            return super()._handle_pdf(item, attachment_path)


_FETCHERS = {
    "html": HtmlFetcher,
    "json": JsonFetcher,
    "auth": AuthFetcher,
}


def build_fetcher(platform: dict) -> BaseFetcher:
    return _FETCHERS[platform["kind"]](platform)
