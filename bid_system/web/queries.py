"""控制台查询：公告筛选、变更记录、任务历史。"""
from __future__ import annotations

from .. import db

PAGE_SIZE = 15


def _arg(args, key: str, default: str = "") -> str:
    """同时兼容 Flask MultiDict、普通 dict 与 Werkzeug 带 type 的取值方式。"""
    value = args.get(key, default)
    return default if value is None else str(value)


def _int_arg(args, key: str, default: int) -> int:
    raw = _arg(args, key, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _notice_filters(args) -> tuple[str, list, dict]:
    where, params, clean = [], [], {}
    region = _arg(args, "region").strip()
    keyword = _arg(args, "keyword").strip()
    min_amt = _arg(args, "min_amount").strip()
    max_amt = _arg(args, "max_amount").strip()
    ptype = _arg(args, "notice_type").strip()

    if region:
        where.append("n.region LIKE ?")
        params.append(f"%{region}%")
        clean["region"] = region
    if keyword:
        where.append("(n.title LIKE ? OR p.project_code LIKE ? OR n.purchaser LIKE ?)")
        params.extend([f"%{keyword}%", f"%{keyword}%", f"%{keyword}%"])
        clean["keyword"] = keyword
    if min_amt:
        where.append("n.budget >= ?")
        params.append(float(min_amt))
        clean["min_amount"] = min_amt
    if max_amt:
        where.append("n.budget <= ?")
        params.append(float(max_amt))
        clean["max_amount"] = max_amt
    if ptype:
        where.append("n.notice_type = ?")
        params.append(ptype)
        clean["notice_type"] = ptype
    return (" WHERE " + " AND ".join(where)) if where else "", params, clean


def list_notices(args) -> tuple[list[dict], int, int, dict]:
    where_sql, params, clean = _notice_filters(args)
    page = max(1, _int_arg(args, "page", 1))
    with db.get_conn() as conn:
        total = conn.execute(
            f"SELECT COUNT(*) AS c FROM notice n JOIN project p ON p.id=n.project_id{where_sql}",
            params,
        ).fetchone()["c"]
        rows = conn.execute(
            f"""SELECT n.*, p.project_key, p.project_code,
                (SELECT COUNT(*) FROM notice c WHERE c.parent_notice_id=n.id) AS child_count
                FROM notice n JOIN project p ON p.id=n.project_id
                {where_sql}
                ORDER BY n.published_at DESC, n.id DESC
                LIMIT ? OFFSET ?""",
            params + [PAGE_SIZE, (page - 1) * PAGE_SIZE],
        ).fetchall()
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    return [dict(r) for r in rows], total, pages, clean


def list_regions() -> list[str]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT region FROM notice WHERE region IS NOT NULL "
            "ORDER BY region").fetchall()
    return [r["region"] for r in rows]


def list_changes(page: int = 1) -> tuple[list[dict], int]:
    with db.get_conn() as conn:
        total = conn.execute("SELECT COUNT(*) AS c FROM field_change").fetchone()["c"]
        rows = conn.execute(
            """SELECT fc.*, n.title AS notice_title, n.platform_code,
                      p.title AS project_title, p.project_code
               FROM field_change fc
               JOIN notice n ON n.id = fc.notice_id
               JOIN project p ON p.id = fc.project_id
               ORDER BY fc.changed_at DESC, fc.id DESC
               LIMIT ? OFFSET ?""",
            (PAGE_SIZE, (max(1, page) - 1) * PAGE_SIZE),
        ).fetchall()
    return [dict(r) for r in rows], max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)


def list_runs(limit: int = 30) -> list[dict]:
    with db.get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM task_run ORDER BY id DESC LIMIT ?", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def project_detail(project_id: int) -> dict | None:
    with db.get_conn() as conn:
        proj = conn.execute("SELECT * FROM project WHERE id=?", (project_id,)).fetchone()
        if not proj:
            return None
        notices = conn.execute(
            "SELECT * FROM notice WHERE project_id=? ORDER BY published_at ASC, id ASC",
            (project_id,),
        ).fetchall()
        changes = conn.execute(
            "SELECT * FROM field_change WHERE project_id=? ORDER BY id ASC",
            (project_id,),
        ).fetchall()
    return {"project": dict(proj),
            "notices": [dict(n) for n in notices],
            "changes": [dict(c) for c in changes]}


def notices_for_export(args) -> list[dict]:
    where_sql, params, _ = _notice_filters(args)
    with db.get_conn() as conn:
        rows = conn.execute(
            f"""SELECT p.project_code AS 项目编号, n.title AS 公告标题,
                       n.notice_type AS 公告类型, n.platform_name AS 来源平台,
                       n.region AS 地区, n.budget AS 预算金额元,
                       n.deadline AS 投标截止时间, n.purchaser AS 采购人,
                       n.published_at AS 发布时间, n.crawled_at AS 采集时间,
                       n.url AS 原文链接
                FROM notice n JOIN project p ON p.id=n.project_id
                {where_sql}
                ORDER BY n.published_at DESC, n.id DESC""",
            params,
        ).fetchall()
    return [dict(r) for r in rows]
