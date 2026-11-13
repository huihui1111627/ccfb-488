"""采集引擎：任务编排、断点续跑、失败重试队列、运行统计。"""
from __future__ import annotations

import threading

from .. import config, db
from ..utils import get_logger
from .fetchers import BaseFetcher, build_fetcher
from .repo import upsert_notice

log = get_logger("engine")

# 同一时刻只允许一个采集任务，避免水位/会话竞争
_run_lock = threading.Lock()


# ----------------------------------------------------------------- 重试队列
def enqueue_retry(platform_code: str, item_id: str | None, stage: str,
                  url: str | None, reason: str) -> None:
    with db.get_conn() as conn:
        # 同平台同条目同阶段只保留一条 pending，避免重复堆积
        row = conn.execute(
            "SELECT id FROM retry_item WHERE platform_code=? AND "
            "COALESCE(platform_notice_id,'')=COALESCE(?, '') AND stage=? AND status='pending'",
            (platform_code, item_id, stage),
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE retry_item SET reason=?, last_error=?, updated_at=? WHERE id=?",
                (reason, reason, db.now_iso(), row["id"]),
            )
        else:
            conn.execute(
                "INSERT INTO retry_item(platform_code,platform_notice_id,stage,url,reason,"
                "status,created_at,updated_at) VALUES(?,?,?,?,?,'pending',?,?)",
                (platform_code, item_id, stage, url, reason,
                 db.now_iso(), db.now_iso()),
            )
        conn.commit()


def _start_run(task_type: str, platform_code: str | None, trigger: str) -> int:
    with db.get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO task_run(task_type,platform_code,status,started_at,trigger) "
            "VALUES(?,?, 'running', ?,?)",
            (task_type, platform_code, db.now_iso(), trigger),
        )
        conn.commit()
        return cur.lastrowid


def _finish_run(run_id: int, stats: dict, status: str, message: str = "") -> None:
    with db.get_conn() as conn:
        conn.execute(
            "UPDATE task_run SET status=?,finished_at=?,total=?,inserted=?,corrected=?,"
            "duplicated=?,failed=?,message=? WHERE id=?",
            (status, db.now_iso(), stats["total"], stats["inserted"], stats["corrected"],
             stats["duplicated"], stats["failed"], message, run_id),
        )
        conn.commit()


# ----------------------------------------------------------------- 主流程
def run_task(mode: str, platform_code: str | None = None, *,
             trigger: str = "manual") -> dict:
    """执行一次采集任务。mode: full / incremental。

    * full：忽略增量水位从第 1 页重翻，已入库公告走唯一键判定为 duplicated，不重复入库。
    * incremental：从 checkpoint 水位与 seen_ids 恢复，只处理新内容。
    * 单条失败不影响整体：写入 retry_item，任务继续，最终状态 partial。
    """
    if not _run_lock.acquire(blocking=False):
        raise RuntimeError("已有采集任务在执行，请等待其结束")
    run_id = _start_run(mode, platform_code, trigger)
    stats = dict(total=0, inserted=0, corrected=0, duplicated=0, failed=0)
    messages: list[str] = []
    try:
        targets = [p for p in config.PLATFORMS
                   if platform_code is None or p["code"] == platform_code]
        for platform in targets:
            try:
                _run_platform(platform, mode, stats)
            except Exception as exc:  # 平台级故障（登录彻底失败等）
                log.exception("平台 %s 采集失败", platform["code"])
                stats["failed"] += 1
                messages.append(f"{platform['code']}: {exc}")
                enqueue_retry(platform["code"], None, "list",
                              platform["base_url"], f"平台级故障: {exc}")
        status = "success"
        if stats["failed"]:
            status = "partial"
        _finish_run(run_id, stats, status, "; ".join(messages))
        log.info("任务 #%s 结束 status=%s 统计=%s", run_id, status, stats)
        return {"run_id": run_id, "status": status, "stats": stats,
                "messages": messages}
    except Exception as exc:
        _finish_run(run_id, stats, "failed", str(exc))
        raise
    finally:
        _run_lock.release()


def _run_platform(platform: dict, mode: str, stats: dict) -> None:
    code = platform["code"]
    checkpoint = db.get_checkpoint(code)
    if mode == "full":
        # 全量重翻：页码从 1 开始，但保留 seen 水位用于断点；不删 checkpoint，
        # 这样中途崩溃仍能从最后页码续跑，已处理条目由唯一键幂等挡住。
        log.info("[%s] 全量采集开始", code)
    else:
        log.info("[%s] 增量采集开始，水位=%s", code,
                 checkpoint["last_published"] if checkpoint else None)

    fetcher = build_fetcher(platform)
    max_published: str | None = checkpoint["last_published"] if checkpoint else None
    try:
        for item, doc in fetcher.iter_items(mode, checkpoint):
            stats["total"] += 1
            try:
                action = upsert_notice(doc)
                stats[action] = stats.get(action, 0) + 1
                if action in ("inserted", "corrected"):
                    log.info("[%s] %s <- %s (%s)", code, item.platform_notice_id,
                             action, doc["title"][:30])
                if doc.get("published_at"):
                    if not max_published or doc["published_at"] > max_published:
                        max_published = doc["published_at"]
                # 每条成功后推进水位与断点，崩溃后从下一页继续
                cp = db.get_checkpoint(code)
                seen = set()
                if cp:
                    import json as _json
                    seen = set(_json.loads(cp["seen_ids"] or "[]"))
                seen.add(item.platform_notice_id)
                db.upsert_checkpoint(code, cp["last_page"] if cp else 1,
                                     max_published, seen)
            except Exception as exc:
                stats["failed"] += 1
                log.exception("[%s] 条目 %s 处理失败，进重试队列", code,
                              item.platform_notice_id)
                enqueue_retry(code, item.platform_notice_id, "detail",
                              item.detail_url, f"{type(exc).__name__}: {exc}")
    finally:
        fetcher.close()
        if max_published:
            cp = db.get_checkpoint(code)
            import json as _json
            seen = set(_json.loads(cp["seen_ids"] or "[]")) if cp else set()
            db.upsert_checkpoint(code, 1, max_published, seen)


# ----------------------------------------------------------------- 重试处理
def list_retry_items(status: str | None = None) -> list:
    sql = "SELECT * FROM retry_item"
    args: tuple = ()
    if status:
        sql += " WHERE status=?"
        args = (status,)
    sql += " ORDER BY updated_at DESC"
    with db.get_conn() as conn:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]


def retry_one(item_id: int) -> dict:
    """手动重跑单条失败条目。"""
    with db.get_conn() as conn:
        row = conn.execute("SELECT * FROM retry_item WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise ValueError("重试条目不存在")
        item = dict(row)
        if item["attempts"] >= config.MAX_RETRY_QUEUE:
            conn.execute(
                "UPDATE retry_item SET status='dead', updated_at=? WHERE id=?",
                (db.now_iso(), item_id))
            conn.commit()
            return {"ok": False, "reason": "超过最大尝试次数，标记为 dead"}
        conn.execute("UPDATE retry_item SET status='running', attempts=attempts+1, "
                     "updated_at=? WHERE id=?", (db.now_iso(), item_id))
        conn.commit()

    platform = next(p for p in config.PLATFORMS if p["code"] == item["platform_code"])
    try:
        result = _reprocess_item(platform, item["platform_notice_id"])
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE retry_item SET status='success', last_error=NULL, updated_at=? "
                "WHERE id=?", (db.now_iso(), item_id))
            conn.commit()
        return {"ok": True, "action": result}
    except Exception as exc:
        reason = f"{type(exc).__name__}: {exc}"
        with db.get_conn() as conn:
            conn.execute(
                "UPDATE retry_item SET status=CASE WHEN attempts>=? THEN 'dead' "
                "ELSE 'pending' END, last_error=?, updated_at=? WHERE id=?",
                (config.MAX_RETRY_QUEUE, reason, db.now_iso(), item_id))
            conn.commit()
        return {"ok": False, "reason": reason}


def retry_all_pending() -> dict:
    items = list_retry_items("pending")
    ok, fail = 0, 0
    for item in items:
        r = retry_one(item["id"])
        ok += int(r["ok"])
        fail += int(not r["ok"])
    return {"total": len(items), "success": ok, "failed": fail}


def _reprocess_item(platform: dict, notice_id: str | None) -> str:
    """重新走一遍 列表定位 -> 详情 -> PDF -> 落库。"""
    if not notice_id:
        # 平台级故障：直接触发一次增量
        return run_task("incremental", platform["code"], trigger="manual_retry")["status"]
    fetcher: BaseFetcher = build_fetcher(platform)
    try:
        target = None
        page = 1
        while True:
            items, total = fetcher.list_page(page)
            for it in items:
                if it.platform_notice_id == notice_id:
                    target = it
                    break
            if target or page * 5 >= total or not items:
                break
            page += 1
        if not target:
            raise RuntimeError(f"平台列表中已找不到条目 {notice_id}（可能被撤回）")
        payload = fetcher.fetch_detail(target)
        doc = fetcher.standard_doc(target, payload["detail"],
                                   payload["pdf_path"], payload["pdf_fields"])
        return upsert_notice(doc)
    finally:
        fetcher.close()


def is_busy() -> bool:
    return _run_lock.locked()
