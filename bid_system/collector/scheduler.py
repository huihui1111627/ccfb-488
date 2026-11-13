"""轻量定时调度器：后台线程按 5 段 cron 表达式触发增量采集。

不引入 APScheduler 依赖。支持的表达式子集（覆盖控制台所需）：
  */N * * * *      每 N 分钟
  M,H * * * *      指定分钟/小时逗号列表
  * * * * *        每分钟
任务重叠时跳过（引擎自身也有互斥锁双保险）。
"""
from __future__ import annotations

import threading
from datetime import datetime

from .. import config, db
from ..utils import get_logger
from . import engine

log = get_logger("scheduler")


def _parse_field(expr: str, min_v: int, max_v: int) -> set[int]:
    values: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if part == "*":
            values.update(range(min_v, max_v + 1))
        elif part.startswith("*/"):
            step = int(part[2:])
            values.update(range(min_v, max_v + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            values.update(range(int(a), int(b) + 1))
        else:
            values.add(int(part))
    return values


def cron_matches(expr: str, dt: datetime) -> bool:
    try:
        m, h, dom, mon, dow = expr.split()
    except ValueError:
        return False
    return (
        dt.minute in _parse_field(m, 0, 59)
        and dt.hour in _parse_field(h, 0, 23)
        and dt.day in _parse_field(dom, 1, 31)
        and dt.month in _parse_field(mon, 1, 12)
        and (dt.isoweekday() % 7) in _parse_field(dow, 0, 6)
    )


class Scheduler:
    def __init__(self) -> None:
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_fire_minute: tuple[int, int, int, int] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="bid-scheduler",
                                        daemon=True)
        self._thread.start()
        log.info("调度器已启动，轮询间隔 %ss", config.SCHEDULER_POLL_SECONDS)

    def stop(self) -> None:
        self._stop.set()

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _loop(self) -> None:
        while not self._stop.wait(config.SCHEDULER_POLL_SECONDS):
            try:
                settings = db.get_settings()
                if settings.get("scheduler_enabled") != "1":
                    continue
                now = datetime.now()
                minute_key = (now.year, now.month, now.day,
                              now.hour * 60 + now.minute)
                if minute_key == self._last_fire_minute:
                    continue
                if cron_matches(settings.get("schedule_cron", "*/30 * * * *"), now):
                    self._last_fire_minute = minute_key
                    if engine.is_busy():
                        log.info("调度触发但引擎忙，本次跳过")
                        continue
                    log.info("定时增量采集触发 cron=%s", settings["schedule_cron"])
                    engine.run_task("incremental", trigger="scheduled")
            except Exception:
                log.exception("调度循环异常")


SCHEDULER = Scheduler()
