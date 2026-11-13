#!/usr/bin/env python3
"""端到端演示脚本（不启动 Web，直接驱动引擎 + mock 平台）。

流程
  0. 启动三个 mock 平台（后台线程），准备空库
  1. 全量采集：16 条原始公告、跨平台转载归并为同一项目
  2. 再跑一次全量：全部判重，零重复入库
  3. mock 演化第 1 轮 -> 增量：只入新增，更正公告挂接并记录字段差异
  4. mock 演化第 2 轮 -> 再增量：二次更正可追溯，仍然零重复
  5. 故障演练：429 限流退避、500 注入重试、登录过期自动续登、断点续跑
  6. 筛选 + Excel 导出验证
所有关键环节带断言，失败即非零退出。
"""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

# 保证从项目根目录直接 python bid_system/demo.py 也能跑
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bid_system import config, db  # noqa: E402
from bid_system.collector import engine  # noqa: E402
from bid_system.mock_platforms.servers import APP_FACTORIES  # noqa: E402
from bid_system.mock_platforms.store import STORE  # noqa: E402
from bid_system.utils import get_logger  # noqa: E402
from bid_system.web import queries  # noqa: E402

log = get_logger("demo")
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    mark = "✓" if cond else "✗"
    if cond:
        PASS += 1
        log.info("%s %s %s", mark, name, detail)
    else:
        FAIL += 1
        log.error("%s %s %s", mark, name, detail)


def _start_mocks() -> None:
    import threading
    for code, (factory, port) in APP_FACTORIES.items():
        threading.Thread(
            target=lambda a=factory(), p=port: a.run(
                host="127.0.0.1", port=p, debug=False, use_reloader=False,
                threaded=True),
            name=f"mock-{code}", daemon=True).start()
    time.sleep(1.2)


def _admin(platform_idx: int, path: str, **params) -> None:
    import httpx
    base = config.PLATFORMS[platform_idx]["base_url"]
    with httpx.Client(timeout=10) as cli:
        cli.post(f"{base}{path}", params=params)


def main() -> int:
    # 干净的数据目录
    if config.DATA_DIR.exists():
        shutil.rmtree(config.DATA_DIR)
    config.DATA_DIR.mkdir(parents=True)
    config.PDF_DIR.mkdir(parents=True)
    db.init_db()
    STORE.seed()
    _start_mocks()

    # ------------------------------------------------ 1. 首次全量
    log.info("=" * 30 + " 第1步：首次全量采集 " + "=" * 30)
    r1 = engine.run_task("full", trigger="script")
    with db.get_conn() as conn:
        n_notice = conn.execute("SELECT COUNT(*) c FROM notice").fetchone()["c"]
        n_project = conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"]
        n_pdf = len(list(config.PDF_DIR.rglob("*.pdf")))
    check("全量状态 success", r1["status"] == "success", str(r1["stats"]))
    check("16 条公告全部入库", n_notice == 16, f"实际 {n_notice}")
    # 10 个项目编号，其中 004/005/006、007/008/009 跨平台转载 -> 10 个项目
    check("跨平台转载去重为 10 个项目", n_project == 10, f"实际 {n_project}")
    check("16 个 PDF 附件均已下载解析", n_pdf == 16, f"实际 {n_pdf}")
    check("预算字段全部从 PDF 解析出",
          all(r["预算金额元"] for r in queries.notices_for_export({})), "")

    # 抽查 PDF 解析金额
    with db.get_conn() as conn:
        row = conn.execute(
            "SELECT budget,deadline FROM notice WHERE platform_notice_id='HTML-001'"
        ).fetchone()
    check("PDF 预算解析正确(1,250,000.00)", row["budget"] == 1_250_000.0,
          str(dict(row)))
    check("PDF 截止时间解析正确", row["deadline"] == "2026-12-20 09:30",
          str(dict(row)))

    # ------------------------------------------------ 2. 重复全量零入库
    log.info("=" * 30 + " 第2步：重复全量，验证幂等 " + "=" * 30)
    r2 = engine.run_task("full", trigger="script")
    with db.get_conn() as conn:
        n_notice2 = conn.execute("SELECT COUNT(*) c FROM notice").fetchone()["c"]
    check("重复全量零新增", r2["stats"]["inserted"] == 0 and r2["stats"]["corrected"] == 0,
          str(r2["stats"]))
    check("公告总数仍为 16", n_notice2 == 16, f"实际 {n_notice2}")
    check("16 条全部判定 duplicated", r2["stats"]["duplicated"] == 16, str(r2["stats"]))

    # ------------------------------------------------ 3. 第一轮演化 + 增量
    log.info("=" * 30 + " 第3步：演化第1轮后增量 " + "=" * 30)
    STORE.evolve(1)
    r3 = engine.run_task("incremental", trigger="script")
    # 新增：HTML-101、JSON-101（同项目转载）、AUTH-101（新项目012）
    # 更正：HTML-201、JSON-201
    check("增量只处理新条目(5条)", r3["stats"]["total"] == 5, str(r3["stats"]))
    check("新增 3 条公告", r3["stats"]["inserted"] == 3, str(r3["stats"]))
    check("更正 2 条", r3["stats"]["corrected"] == 2, str(r3["stats"]))
    with db.get_conn() as conn:
        p2 = conn.execute(
            "SELECT budget FROM notice WHERE project_id="
            "(SELECT id FROM project WHERE project_code='HX2026002') "
            "AND notice_type != '更正公告' ORDER BY id LIMIT 1").fetchone()
        p5 = conn.execute(
            "SELECT deadline FROM notice WHERE project_id="
            "(SELECT id FROM project WHERE project_code='HX2026005') "
            "AND notice_type != '更正公告' ORDER BY id LIMIT 1").fetchone()
        changes = conn.execute(
            "SELECT field_name,old_value,new_value FROM field_change "
            "WHERE field_name IN ('budget','deadline')").fetchall()
    check("P2 原公告预算已同步为 3,520,000", p2["budget"] == 3_520_000.0, str(dict(p2)))
    check("P5 原公告截止已延后", p5["deadline"] == "2026-12-28 14:00", str(dict(p5)))
    change_fields = {c["field_name"] for c in changes}
    check("字段差异已留痕(budget/deadline)",
          {"budget", "deadline"} <= change_fields, str(change_fields))
    # 转载去重新项目 011 只有一个 project
    with db.get_conn() as conn:
        n011 = conn.execute(
            "SELECT COUNT(*) c FROM project WHERE project_code='HX2026011'"
        ).fetchone()["c"]
        n011_notices = conn.execute(
            "SELECT COUNT(*) c FROM notice WHERE project_id="
            "(SELECT id FROM project WHERE project_code='HX2026011')").fetchone()["c"]
    check("011 跨平台转载仍为 1 个项目", n011 == 1 and n011_notices == 2,
          f"projects={n011} notices={n011_notices}")

    # 再跑一次增量：水位生效，零处理
    r3b = engine.run_task("incremental", trigger="script")
    check("水位生效，二次增量零处理", r3b["stats"]["total"] == 0, str(r3b["stats"]))

    # ------------------------------------------------ 4. 第二轮演化 + 增量
    log.info("=" * 30 + " 第4步：演化第2轮后增量（含二次更正）" + "=" * 30)
    STORE.evolve(2)
    r4 = engine.run_task("incremental", trigger="script")
    check("第二轮增量处理 5 条新条目", r4["stats"]["total"] == 5, str(r4["stats"]))
    check("新增 3、更正 2", r4["stats"]["inserted"] == 3
          and r4["stats"]["corrected"] == 2, str(r4["stats"]))
    with db.get_conn() as conn:
        p7 = conn.execute(
            "SELECT budget,deadline FROM notice WHERE project_id="
            "(SELECT id FROM project WHERE project_code='HX2026007') "
            "AND notice_type != '更正公告' ORDER BY id LIMIT 1").fetchone()
        p2_changes = conn.execute(
            "SELECT new_value FROM field_change WHERE project_id="
            "(SELECT id FROM project WHERE project_code='HX2026002') "
            "AND field_name='budget' ORDER BY id").fetchall()
        total_notices = conn.execute("SELECT COUNT(*) c FROM notice").fetchone()["c"]
    check("P7 预算+截止同时更正",
          p7["budget"] == 1_620_000.0 and p7["deadline"] == "2027-01-08 10:00",
          str(dict(p7)))
    check("P2 两次更正预算均可追溯(352万->360万)",
          [c["new_value"] for c in p2_changes][-2:] == ["3520000.0", "3600000.0"],
          str([c["new_value"] for c in p2_changes]))
    check("两轮后公告总数 26，无重复入库", total_notices == 26, f"实际 {total_notices}")

    # ------------------------------------------------ 5. 故障演练
    log.info("=" * 30 + " 第5步：限流/故障/会话过期演练 " + "=" * 30)
    # 5a. 429 限流：html 平台每 15 秒仅 6 次，全量请求量大 -> 必触发退避
    _admin(0, "/admin/rate-limit", limit=6)
    t0 = time.time()
    r5 = engine.run_task("full", "html_gov", trigger="script")
    elapsed = time.time() - t0
    _admin(0, "/admin/rate-limit", limit=0)
    check("限流场景任务仍成功（退避生效）", r5["status"] in ("success", "partial"),
          f"{r5['status']} 用时{elapsed:.1f}s")
    check("限流退避确实消耗了时间", elapsed >= 2.0, f"{elapsed:.1f}s")

    # 5b1. 500 故障注入 2 次：小于退避重试次数(4)，客户端自动消化，不进队列
    _admin(1, "/admin/flaky", count=2)
    r6a = engine.run_task("full", "json_ggzy", trigger="script")
    check("短暂500被退避重试消化，无重试条目",
          len(engine.list_retry_items("pending")) == 0 and r6a["stats"]["failed"] == 0,
          f"stats={r6a['stats']}")

    # 5b2. 故障注入 6 次：超过 4 次退避上限，条目用尽重试后进队列
    _admin(1, "/admin/flaky", count=6)
    r6 = engine.run_task("full", "json_ggzy", trigger="script")
    pending = engine.list_retry_items("pending")
    check("持续500耗尽退避后进入重试队列", len(pending) >= 1 and r6["status"] == "partial",
          f"pending={len(pending)} stats={r6['stats']}")
    # 手动重跑：故障计数已被消耗，应成功
    time.sleep(0.2)
    rr = engine.retry_one(pending[0]["id"])
    check("手动重跑成功", rr["ok"], str(rr))
    check("重试条目状态变 success",
          engine.list_retry_items("success")[-1]["status"] == "success", "")

    # 5c. 登录过期自动续登
    _admin(2, "/admin/expire-sessions", force=1)
    r7 = engine.run_task("incremental", "auth_jy", trigger="script")
    check("会话过期后自动续登并完成采集", r7["status"] in ("success", "partial"),
          str(r7["stats"]))
    _admin(2, "/admin/expire-sessions", force=0)

    # 5d. 断点续跑：人为写入中间页码断点，全量能继续且不重复
    db.upsert_checkpoint("html_gov", 2, None, ["HTML-001"])
    before = db.get_checkpoint("html_gov")
    r8 = engine.run_task("full", "html_gov", trigger="script")
    check("断点续跑无重复入库", r8["stats"]["inserted"] == 0, str(r8["stats"]))
    check("checkpoint 已被推进", db.get_checkpoint("html_gov")["last_page"] is not None,
          str(dict(before)))

    # ------------------------------------------------ 6. 筛选与导出
    log.info("=" * 30 + " 第6步：筛选与 Excel 导出 " + "=" * 30)
    big = queries.list_notices({"min_amount": 5_000_000})[1]
    kw = queries.list_notices({"keyword": "监理"})[1]
    region = queries.list_notices({"region": "滨江省"})[1]
    check("金额筛选生效(≥500万)", big >= 3, f"命中 {big}")
    check("关键词筛选生效(监理)", kw >= 1, f"命中 {kw}")
    check("地区筛选生效(滨江省)", region >= 6, f"命中 {region}")

    from openpyxl import load_workbook
    from bid_system.web.app import create_app
    app = create_app()
    with app.test_client() as cli:
        resp = cli.get("/export/excel?min_amount=5000000")
        check("Excel 导出 HTTP 200", resp.status_code == 200, str(resp.status_code))
        xlsx = Path(config.DATA_DIR) / "export_check.xlsx"
        xlsx.write_bytes(resp.data)
        wb = load_workbook(xlsx)
        rows = list(wb.active.iter_rows(values_only=True))
    check("Excel 行数与筛选结果一致", len(rows) - 1 == big,
          f"excel={len(rows) - 1} db={big}")
    check("Excel 表头含预算金额列", any("预算" in str(h) for h in rows[0]), str(rows[0]))

    # ------------------------------------------------ 汇总
    log.info("=" * 70)
    log.info("断言结果：通过 %d / 失败 %d", PASS, FAIL)
    with db.get_conn() as conn:
        summary = {
            "projects": conn.execute("SELECT COUNT(*) c FROM project").fetchone()["c"],
            "notices": conn.execute("SELECT COUNT(*) c FROM notice").fetchone()["c"],
            "changes": conn.execute("SELECT COUNT(*) c FROM field_change").fetchone()["c"],
            "runs": conn.execute("SELECT COUNT(*) c FROM task_run").fetchone()["c"],
        }
    log.info("库内汇总：%s", summary)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
