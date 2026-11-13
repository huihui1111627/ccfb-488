"""三个 mock 平台共享的数据仓库与故障/会话控制。

数据演化（演示增量采集）
    seed()  初始 10 个项目、跨平台转载重叠
    evolve(1) 每平台新增公告 + 对 P2/P5 发更正（预算、截止时间变化）
    evolve(2) 再新增 + 对 P7 发更正（两个字段同时变）+ 新项目跨平台转载

所有平台条目 id 全局稳定，重复采集不会产生新数据。
"""
from __future__ import annotations

import threading
import uuid
from collections import defaultdict, deque
from datetime import datetime, timedelta
from typing import Any

from .pdf_doc import build_notice_pdf

PAGE_SIZE = 5

# 项目主数据：项目编号 -> 基础信息（多平台转载共享同一编号）
_PROJECTS = {
    "HX-2026-001": ("市政道路路灯节能改造工程", "华信市", "华信市城管局"),
    "HX-2026-002": ("第一人民医院医疗设备采购项目", "华信市", "华信市第一人民医院"),
    "HX-2026-003": ("智慧园区物联网平台建设项目", "华信市", "华信市高新区管委会"),
    "HX-2026-004": ("河道综合治理工程勘察设计", "滨江省", "滨江省水利厅"),
    "HX-2026-005": ("省级政务云扩容服务采购", "滨江省", "滨江省政务服务局"),
    "HX-2026-006": ("农产品冷链物流园施工总承包", "滨江省", "滨江省农业农村厅"),
    "HX-2026-007": ("城市快速路南延工程监理服务", "重点区", "重点区交通建设中心"),
    "HX-2026-008": ("数据中心机房UPS及配电采购", "重点区", "重点区大数据局"),
    "HX-2026-009": ("学校食堂食材定点配送服务", "滨江省", "滨江省教育厅"),
    "HX-2026-010": ("老旧小区改造配套基础设施", "重点区", "重点区住建局"),
}

# 平台 -> 承载的项目编号（刻意重叠以演示转载去重）
_PLATFORM_PROJECTS = {
    "html_gov": ["HX-2026-001", "HX-2026-002", "HX-2026-003", "HX-2026-004",
                 "HX-2026-005", "HX-2026-006"],
    "json_ggzy": ["HX-2026-004", "HX-2026-005", "HX-2026-006", "HX-2026-007",
                  "HX-2026-008", "HX-2026-009"],
    "auth_jy": ["HX-2026-007", "HX-2026-008", "HX-2026-009", "HX-2026-010"],
}

_BASE_BUDGET = {
    "HX-2026-001": 1_250_000.0, "HX-2026-002": 3_860_000.0,
    "HX-2026-003": 2_100_000.0, "HX-2026-004": 4_580_000.0,
    "HX-2026-005": 6_720_000.0, "HX-2026-006": 9_950_000.0,
    "HX-2026-007": 1_480_000.0, "HX-2026-008": 2_360_000.0,
    "HX-2026-009": 760_000.0,   "HX-2026-010": 5_240_000.0,
}


class MockStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        # platform_code -> list[notice dict]，按发布时间升序
        self.notices: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.evolved_round = 0
        # 限流：platform -> ip -> 时间戳滑窗
        self.rate_limits: dict[str, int] = {}
        self._hits: dict[tuple[str, str], deque[float]] = defaultdict(deque)
        # 故障注入：platform -> 剩余详情请求失败次数
        self.flaky_remaining: dict[str, int] = defaultdict(int)
        # 登录会话：token -> 过期时间
        self.sessions: dict[str, datetime] = {}
        self.force_expire = False
        self.session_ttl = timedelta(minutes=30)
        self.seed()

    # ------------------------------------------------------------- 数据播种
    def seed(self) -> None:
        with self._lock:
            self.notices.clear()
            self.evolved_round = 0
            base_day = datetime(2026, 11, 5, 9, 0)
            for platform, codes in _PLATFORM_PROJECTS.items():
                prefix = {"html_gov": "HTML", "json_ggzy": "JSON", "auth_jy": "AUTH"}[platform]
                for i, code in enumerate(codes, start=1):
                    title, region, purchaser = _PROJECTS[code]
                    self.notices[platform].append({
                        "id": f"{prefix}-{i:03d}",
                        "project_code": code,
                        "title": f"{title}招标公告",
                        "region": region,
                        "purchaser": purchaser,
                        "type": "招标公告",
                        "published_at": (base_day + timedelta(days=i, hours=i)).strftime(
                            "%Y-%m-%d %H:%M:%S"),
                        "budget": _BASE_BUDGET[code],
                        "deadline": "2026-12-20 09:30",
                        "content": "本项目接受联合体投标，资格要求详见招标文件第三章。",
                    })

    def evolve(self, round_no: int) -> dict[str, int]:
        """模拟平台上新：新增公告 + 更正公告。返回各平台新增条数。"""
        with self._lock:
            if round_no <= self.evolved_round:
                return {p: 0 for p in self.notices}
            ts = datetime(2026, 11, 10 + round_no, 10, 0).strftime("%Y-%m-%d %H:%M:%S")
            added: dict[str, int] = defaultdict(int)

            def add(platform: str, nid: str, code: str, ntype: str, **over: Any) -> None:
                title, region, purchaser = _PROJECTS[code]
                tag = "更正公告" if ntype == "更正公告" else "招标公告"
                doc = {
                    "id": nid, "project_code": code,
                    "title": f"{title}{tag}", "region": region,
                    "purchaser": purchaser, "type": ntype,
                    "published_at": ts, "budget": _BASE_BUDGET[code],
                    "deadline": "2026-12-20 09:30",
                    "content": "本项目接受联合体投标，资格要求详见招标文件第三章。",
                }
                doc.update(over)
                # 同平台 id 幂等
                if not any(n["id"] == nid for n in self.notices[platform]):
                    self.notices[platform].append(doc)
                    added[platform] += 1

            if round_no == 1:
                # 新增：每平台一条新项目/新公告
                _PROJECTS.setdefault("HX-2026-011", ("水库除险加固工程", "滨江省", "滨江省水利厅"))
                _BASE_BUDGET.setdefault("HX-2026-011", 3_120_000.0)
                add("html_gov", "HTML-101", "HX-2026-011", "招标公告")
                add("json_ggzy", "JSON-101", "HX-2026-011", "招标公告")  # 跨平台转载
                add("auth_jy", "AUTH-101", "HX-2026-012", "招标公告",
                    **self._new_project("HX-2026-012", "重点区消防站装备采购",
                                        "重点区", "重点区消防救援支队", 1_980_000.0))
                # 更正：P2 预算调整（html 平台）
                add("html_gov", "HTML-201", "HX-2026-002", "更正公告",
                    budget=3_520_000.0,
                    content="更正事项：因采购清单调整，预算金额由386万元更正为352万元。")
                # 更正：P5 截止时间延后（json 平台）
                add("json_ggzy", "JSON-201", "HX-2026-005", "更正公告",
                    deadline="2026-12-28 14:00",
                    content="更正事项：投标截止时间由2026-12-20 09:30延后至2026-12-28 14:00。")
            elif round_no == 2:
                add("html_gov", "HTML-102", "HX-2026-013", "招标公告",
                    **self._new_project("HX-2026-013", "城区排水管网检测服务",
                                        "华信市", "华信市水务局", 880_000.0))
                add("json_ggzy", "JSON-102", "HX-2026-013", "招标公告")  # 转载
                add("auth_jy", "AUTH-102", "HX-2026-014", "招标公告",
                    **self._new_project("HX-2026-014", "轨道交通安防系统集成",
                                        "重点区", "重点区轨道交通集团", 7_450_000.0))
                # 更正：P7 预算+截止同时变（auth 平台）
                add("auth_jy", "AUTH-201", "HX-2026-007", "更正公告",
                    budget=1_620_000.0, deadline="2027-01-08 10:00",
                    content="更正事项：监理范围扩大，预算调整为162万元；截止时间顺延。")
                # 对第一轮已更正过的 P2 再次更正（验证多次更正留痕）
                add("html_gov", "HTML-202", "HX-2026-002", "更正公告",
                    budget=3_600_000.0,
                    content="二次更正：预算金额复核后调整为360万元。")

            self.evolved_round = round_no
            return dict(added)

    def _new_project(self, code: str, title: str, region: str, purchaser: str,
                     budget: float) -> dict:
        _PROJECTS.setdefault(code, (title, region, purchaser))
        _BASE_BUDGET.setdefault(code, budget)
        return {}

    # ------------------------------------------------------------- 查询
    def list_notices(self, platform: str, page: int) -> tuple[list[dict], int]:
        with self._lock:
            items = sorted(self.notices[platform],
                           key=lambda n: (n["published_at"], n["id"]))
            total = len(items)
            start = (max(page, 1) - 1) * PAGE_SIZE
            return items[start:start + PAGE_SIZE], total

    def get_notice(self, platform: str, notice_id: str) -> dict | None:
        with self._lock:
            for n in self.notices[platform]:
                if n["id"] == notice_id:
                    return dict(n)
        return None

    def all_ids(self, platform: str) -> list[str]:
        with self._lock:
            return [n["id"] for n in self.notices[platform]]

    def build_pdf(self, platform: str, notice_id: str) -> bytes | None:
        n = self.get_notice(platform, notice_id)
        if not n:
            return None
        region, purchaser = n["region"], n["purchaser"]
        return build_notice_pdf(
            title=n["title"], project_code=n["project_code"], region=region,
            purchaser=purchaser, budget=n["budget"], deadline=n["deadline"],
            content=n["content"],
        )

    # ------------------------------------------------------------- 限流 / 故障
    RATE_WINDOW_SECONDS = 15  # 滑窗窗口，保证客户端退避后配额能恢复

    def check_rate(self, platform: str, ip: str) -> int | None:
        """通过返回 None；触发限流返回建议 Retry-After 秒数。"""
        limit = self.rate_limits.get(platform, 0)
        if not limit:
            return None
        now = datetime.now().timestamp()
        with self._lock:
            q = self._hits[(platform, ip)]
            while q and now - q[0] > self.RATE_WINDOW_SECONDS:
                q.popleft()
            if len(q) >= limit:
                return max(1, int(self.RATE_WINDOW_SECONDS - (now - q[0])))
            q.append(now)
        return None

    def set_rate_limit(self, platform: str, per_minute: int) -> None:
        with self._lock:
            if per_minute > 0:
                self.rate_limits[platform] = per_minute
            else:
                self.rate_limits.pop(platform, None)

    def consume_flaky(self, platform: str) -> bool:
        """返回 True 表示本次详情请求应注入 500 故障。"""
        with self._lock:
            if self.flaky_remaining[platform] > 0:
                self.flaky_remaining[platform] -= 1
                return True
        return False

    def set_flaky(self, platform: str, count: int) -> None:
        with self._lock:
            self.flaky_remaining[platform] = count

    # ------------------------------------------------------------- 登录会话
    def login(self, username: str, password: str) -> str | None:
        # 凭据由 server 层校验，这里只发会话；重新登录即解除强制过期
        token = uuid.uuid4().hex
        with self._lock:
            self.force_expire = False
            self.sessions[token] = datetime.now() + self.session_ttl
        return token

    def check_session(self, token: str | None) -> bool:
        if not token or self.force_expire:
            return False
        with self._lock:
            exp = self.sessions.get(token)
            if not exp:
                return False
            if datetime.now() >= exp:
                self.sessions.pop(token, None)
                return False
        return True

    def drop_sessions(self) -> None:
        with self._lock:
            self.sessions.clear()

    def state(self) -> dict:
        with self._lock:
            return {
                "round": self.evolved_round,
                "counts": {p: len(v) for p, v in self.notices.items()},
                "rate_limits": dict(self.rate_limits),
                "flaky": dict(self.flaky_remaining),
                "force_expire": self.force_expire,
                "sessions": len(self.sessions),
            }


# 三个 server 进程/线程共享同一实例（本项目单进程多线程启动）
STORE = MockStore()
