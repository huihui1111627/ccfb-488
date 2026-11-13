"""全局配置：路径、平台连接信息、采集策略参数。"""
from __future__ import annotations

import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("BID_DATA_DIR", BASE_DIR / "data"))
PDF_DIR = DATA_DIR / "pdfs"
DB_PATH = DATA_DIR / "bids.db"

for _d in (DATA_DIR, PDF_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------- 三个 mock 平台
PLATFORMS = [
    {
        "code": "html_gov",
        "name": "华信市政府采购网(纯HTML分页)",
        "base_url": "http://127.0.0.1:8101",
        "kind": "html",
    },
    {
        "code": "json_ggzy",
        "name": "省公共资源交易中心(JSON接口)",
        "base_url": "http://127.0.0.1:8102",
        "kind": "json",
    },
    {
        "code": "auth_jy",
        "name": "重点工程交易平台(登录会话30分钟)",
        "base_url": "http://127.0.0.1:8103",
        "kind": "auth",
    },
]

# 登录平台凭据（mock 用，真实环境放设置表/环境变量）
AUTH_USERNAME = os.environ.get("BID_AUTH_USER", "consultant")
AUTH_PASSWORD = os.environ.get("BID_AUTH_PASS", "Bid@2026")
AUTH_COOKIE_NAME = "sessionid"
SESSION_TTL_MINUTES = 30

# ---------------------------------------------------------------- 采集策略
HTTP_TIMEOUT = 20.0
# 退避：遇 429/5xx 的重试序列（秒），配合 Retry-After 取较大值
BACKOFF_SCHEDULE = [2, 5, 10, 20]
MAX_RETRY_QUEUE = 50          # 单条目进入重试队列后的最大自动/手动尝试上限
RATE_LIMIT_SLEEP = 0.0        # 平台间礼貌间隔（mock 本地可调 0）

# 调度器
SCHEDULER_POLL_SECONDS = 20

# Web
WEB_HOST = "127.0.0.1"
WEB_PORT = 8200
SECRET_KEY = os.environ.get("BID_SECRET", "dev-secret-change-me")
