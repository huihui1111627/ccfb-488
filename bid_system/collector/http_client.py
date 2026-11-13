"""HTTP 客户端封装：429/5xx 自动指数退避，尊重 Retry-After。"""
from __future__ import annotations

import time

import httpx

from .. import config
from ..utils import get_logger

log = get_logger("http")


class FetchError(Exception):
    """重试用尽后仍失败。"""


class AuthExpiredError(Exception):
    """登录会话过期，需要重新登录后由上层重试。"""


class HttpClient:
    def __init__(self, base_url: str, platform_code: str,
                 cookies: dict[str, str] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.platform_code = platform_code
        self.client = httpx.Client(
            base_url=base_url, timeout=config.HTTP_TIMEOUT,
            follow_redirects=True, cookies=cookies, headers=headers,
        )

    def close(self) -> None:
        self.client.close()

    def __enter__(self) -> "HttpClient":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def cookies(self) -> httpx.Cookies:
        return self.client.cookies

    def request(self, method: str, url: str, *, stream: bool = False,
                **kwargs) -> httpx.Response:
        """带退避重试的请求。stream=True 时返回未关闭的流式响应（调用方负责 close）。"""
        last_err: Exception | None = None
        for attempt, delay in enumerate(config.BACKOFF_SCHEDULE):
            try:
                req = self.client.build_request(method, url, **kwargs)
                resp = self.client.send(req, stream=stream)
            except httpx.HTTPError as exc:  # 连接级故障：DNS/拒连/读超时
                last_err = exc
                log.warning("[%s] %s 连接失败(第%d次): %s，%ss 后重试",
                            self.platform_code, url, attempt + 1, exc, delay)
                time.sleep(delay)
                continue

            if resp.status_code == 401:
                resp.close()
                raise AuthExpiredError(f"{self.platform_code} 会话过期(401)")
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = resp.headers.get("Retry-After")
                wait = self._retry_seconds(resp.status_code, retry_after, delay)
                log.warning("[%s] %s 返回 %s，%ss 后退避重试(第%d/%d次)",
                            self.platform_code, url, resp.status_code, wait,
                            attempt + 1, len(config.BACKOFF_SCHEDULE))
                resp.close()
                time.sleep(wait)
                last_err = FetchError(f"HTTP {resp.status_code}")
                continue
            if resp.status_code >= 400:
                body = resp.text[:200]
                resp.close()
                raise FetchError(f"HTTP {resp.status_code}: {body}")
            return resp

        raise FetchError(f"{self.platform_code} {url} 重试用尽: {last_err}")

    @staticmethod
    def _retry_seconds(status: int, retry_after: str | None, fallback: int) -> float:
        if status == 429 and retry_after:
            try:
                # 本地 mock 给的是秒数；真实平台可能是 HTTP-date
                return max(1.0, min(float(retry_after), 30.0))
            except ValueError:
                pass
        return float(fallback)

    def get(self, url: str, **kwargs) -> httpx.Response:
        return self.request("GET", url, **kwargs)
