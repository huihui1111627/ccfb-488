"""登录平台(auth_jy)的会话获取：Playwright 执行 JS 登录，失败回退 httpx 逆向登录。

mock 登录页的 CSRF 字段由前端 JS 计算（tok_1337）。真实环境里这类字段往往更复杂，
因此主路径使用真实浏览器；当浏览器不可用（未装 chromium/无显示环境）时，
回退到已逆向出的常量直接 POST，并在日志中明确标注。
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .. import config
from ..utils import get_logger

log = get_logger("login")

STATE_FILE = config.DATA_DIR / "auth_state.json"
# 逆向得到的 JS 常量（与 mock_platforms/servers.py 登录页脚本保持一致）
_FALLBACK_CSRF = "tok_1337"


def playwright_login() -> dict[str, str]:
    """启动 headless chromium 完成登录，返回 cookie 字典。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        try:
            page.goto(f"{config.PLATFORMS[2]['base_url']}/login",
                      timeout=config.HTTP_TIMEOUT * 1000)
            page.fill("#username", config.AUTH_USERNAME)
            page.fill("#password", config.AUTH_PASSWORD)
            page.click("#login-form button[type=submit]")
            page.wait_for_url("**/", timeout=config.HTTP_TIMEOUT * 1000)
            # 等待 Set-Cookie 落地
            deadline = time.time() + 10
            cookie = {}
            while time.time() < deadline:
                for c in page.context.cookies():
                    if c["name"] == config.AUTH_COOKIE_NAME:
                        cookie[c["name"]] = c["value"]
                if cookie:
                    break
                page.wait_for_timeout(300)
            if not cookie:
                raise RuntimeError("登录后未获取到会话 Cookie")
            STATE_FILE.write_text(json.dumps(cookie, ensure_ascii=False), encoding="utf-8")
            log.info("Playwright 登录成功，会话已缓存到 %s", STATE_FILE.name)
            return cookie
        finally:
            browser.close()


def httpx_login() -> dict[str, str]:
    """回退方案：直接 POST 逆向出的 CSRF 常量。"""
    import httpx

    log.warning("浏览器不可用，使用 httpx 回退登录（逆向 CSRF 常量）")
    resp = httpx.post(
        f"{config.PLATFORMS[2]['base_url']}/login",
        data={"username": config.AUTH_USERNAME, "password": config.AUTH_PASSWORD,
              "csrft": _FALLBACK_CSRF},
        follow_redirects=False, timeout=config.HTTP_TIMEOUT,
    )
    token = resp.cookies.get(config.AUTH_COOKIE_NAME)
    if resp.status_code not in (302, 303) or not token:
        raise RuntimeError(f"httpx 回退登录失败 status={resp.status_code}")
    cookie = {config.AUTH_COOKIE_NAME: token}
    STATE_FILE.write_text(json.dumps(cookie, ensure_ascii=False), encoding="utf-8")
    return cookie


def load_cached_state() -> dict[str, str] | None:
    path = Path(STATE_FILE)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def login() -> dict[str, str]:
    """登录入口：优先 Playwright，异常时回退 httpx。"""
    try:
        return playwright_login()
    except Exception as exc:  # 浏览器缺失/启动失败等
        log.warning("Playwright 登录失败(%s)，切换回退方案", exc)
        return httpx_login()
