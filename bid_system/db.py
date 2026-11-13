"""SQLite 访问层：建表、连接、基础 DAO。

设计要点
--------
* 所有写操作走短事务，采集器可在任意条目后崩溃，重启靠 notice 唯一键 +
  checkpoint 断点实现幂等续跑。
* 去重分两层：
    - project：业务主键 project_key（项目编号优先，否则 地区+标题指纹）
    - notice：平台内 (platform_code, platform_notice_id) 唯一，同公告不重复入库
* 更正公告不覆盖历史：notice 之间 parent_id 挂接，字段级差异落 field_change，
  原 notice 的可变字段同步更新但每次变化都留痕。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from . import config

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def get_conn(db_path: Path | str | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path or config.DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS project (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_key     TEXT NOT NULL UNIQUE,          -- 去重业务键
    project_code    TEXT,                          -- 项目编号(可能缺失)
    title           TEXT NOT NULL,
    region          TEXT,
    latest_notice_id INTEGER,
    first_seen_at   TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS notice (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id          INTEGER NOT NULL REFERENCES project(id),
    platform_code       TEXT NOT NULL,
    platform_name       TEXT NOT NULL,
    platform_notice_id  TEXT NOT NULL,
    notice_type         TEXT NOT NULL DEFAULT '招标公告',  -- 招标公告/更正公告/中标公告...
    title               TEXT NOT NULL,
    url                 TEXT,
    region              TEXT,
    budget              REAL,                      -- 预算金额(元)
    deadline            TEXT,                      -- 投标截止时间
    purchaser           TEXT,
    content_hash        TEXT NOT NULL,             -- 正文+关键字段指纹，识别内容是否变化
    pdf_path            TEXT,
    published_at        TEXT,
    parent_notice_id    INTEGER REFERENCES notice(id),  -- 更正公告挂接的原公告
    crawled_at          TEXT NOT NULL,
    UNIQUE(platform_code, platform_notice_id)
);
CREATE INDEX IF NOT EXISTS idx_notice_project ON notice(project_id);
CREATE INDEX IF NOT EXISTS idx_notice_published ON notice(published_at);

CREATE TABLE IF NOT EXISTS field_change (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id      INTEGER NOT NULL REFERENCES project(id),
    notice_id       INTEGER NOT NULL REFERENCES notice(id),
    parent_notice_id INTEGER REFERENCES notice(id),
    field_name      TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_change_project ON field_change(project_id);

CREATE TABLE IF NOT EXISTS task_run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_type   TEXT NOT NULL,                     -- full / incremental / manual_retry
    platform_code TEXT,                            -- NULL 表示全部平台
    status      TEXT NOT NULL,                     -- running/success/failed/partial
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    total       INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    corrected   INTEGER DEFAULT 0,
    duplicated  INTEGER DEFAULT 0,
    failed      INTEGER DEFAULT 0,
    message     TEXT,
    trigger     TEXT DEFAULT 'manual'              -- manual/scheduled/script
);

CREATE TABLE IF NOT EXISTS checkpoint (
    platform_code TEXT PRIMARY KEY,
    last_page       INTEGER NOT NULL DEFAULT 1,    -- 列表页断点(续跑页码)
    last_published  TEXT,                          -- 增量水位：已见到的最大发布时间
    seen_ids        TEXT NOT NULL DEFAULT '[]',    -- 本水位周期内见过的平台条目id
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS retry_item (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_code   TEXT NOT NULL,
    platform_notice_id TEXT,
    stage           TEXT NOT NULL,                 -- list/detail/pdf/parse/login
    url             TEXT,
    reason          TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending', -- pending/running/success/dead
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_retry_status ON retry_item(status);

CREATE TABLE IF NOT EXISTS setting (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""

DEFAULT_SETTINGS = {
    "scheduler_enabled": "0",       # 定时任务开关
    "schedule_cron": "*/30 * * * *",  # 默认每30分钟一次增量
    "incremental_on_schedule": "1",
    "rate_limit_per_minute": "0",    # 0=不限；下发给 mock 平台的限流阈值
    "flaky_fail_count": "0",         # mock 平台前 N 次详情请求返回500（演示重试）
    "auth_force_expire": "0",        # 1=强制登录态立即失效（演示续登）
}


def init_db() -> None:
    with _lock, get_conn() as conn:
        conn.executescript(SCHEMA)
        for k, v in DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT OR IGNORE INTO setting(key, value, updated_at) VALUES(?,?,?)",
                (k, v, now_iso()),
            )
        conn.commit()


# ----------------------------------------------------------------- settings
def get_settings() -> dict[str, str]:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM setting").fetchall()
    data = dict(DEFAULT_SETTINGS)
    data.update({r["key"]: r["value"] for r in rows})
    return data


def save_setting(key: str, value: Any) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO setting(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, str(value), now_iso()),
        )
        conn.commit()


# ----------------------------------------------------------------- checkpoints
def get_checkpoint(platform_code: str) -> sqlite3.Row | None:
    with get_conn() as conn:
        return conn.execute(
            "SELECT * FROM checkpoint WHERE platform_code=?", (platform_code,)
        ).fetchone()


def upsert_checkpoint(platform_code: str, page: int, last_published: str | None,
                      seen_ids: Iterable[str]) -> None:
    with _lock, get_conn() as conn:
        conn.execute(
            "INSERT INTO checkpoint(platform_code,last_page,last_published,seen_ids,updated_at) "
            "VALUES(?,?,?,?,?) ON CONFLICT(platform_code) DO UPDATE SET "
            "last_page=excluded.last_page, last_published=excluded.last_published, "
            "seen_ids=excluded.seen_ids, updated_at=excluded.updated_at",
            (platform_code, page, last_published,
             json.dumps(sorted(set(seen_ids)), ensure_ascii=False), now_iso()),
        )
        conn.commit()


def reset_checkpoint(platform_code: str) -> None:
    with _lock, get_conn() as conn:
        conn.execute("DELETE FROM checkpoint WHERE platform_code=?", (platform_code,))
        conn.commit()
