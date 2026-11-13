"""采集结果落库：项目去重、公告写入、更正挂接、字段差异留痕。

所有写入在单个事务里完成，配合 notice 的 (platform, platform_notice_id) 唯一键，
采集任务中断重启时同一公告不会重复入库。
"""
from __future__ import annotations

from typing import Any

from .. import db
from ..utils import content_hash, get_logger, normalize_code, project_key

log = get_logger("repo")


def _find_or_create_project(conn, *, project_code: str | None, region: str | None,
                            title: str) -> int:
    key = project_key(project_code, region, title)
    row = conn.execute("SELECT id FROM project WHERE project_key=?", (key,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO project(project_key,project_code,title,region,first_seen_at,updated_at) "
        "VALUES(?,?,?,?,?,?)",
        (key, normalize_code(project_code) or None, title, region,
         db.now_iso(), db.now_iso()),
    )
    log.info("新项目入库 key=%s title=%s", key[:24], title[:40])
    return cur.lastrowid


def _record_change(conn, *, project_id: int, notice_id: int,
                   parent_id: int | None, field: str, old: Any, new: Any) -> None:
    if old == new:
        return
    conn.execute(
        "INSERT INTO field_change(project_id,notice_id,parent_notice_id,field_name,"
        "old_value,new_value,changed_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, notice_id, parent_id, field,
         None if old is None else str(old),
         None if new is None else str(new), db.now_iso()),
    )


# 可被更正公告修改的字段及其展示名
TRACKED_FIELDS = {
    "title": "公告标题",
    "budget": "预算金额(元)",
    "deadline": "投标截止时间",
    "purchaser": "采购人",
    "region": "地区",
}


def upsert_notice(doc: dict) -> str:
    """落库一条公告，返回动作：inserted / duplicated / corrected。

    doc 字段：platform_code/platform_name/platform_notice_id/notice_type/title/url/
              region/budget/deadline/purchaser/pdf_path/published_at/project_code
    """
    platform = doc["platform_code"]
    nid = str(doc["platform_notice_id"])
    h = content_hash(
        doc.get("title"), doc.get("budget"), doc.get("deadline"),
        doc.get("purchaser"), doc.get("region"), doc.get("notice_type"),
    )

    with db.get_conn() as conn:
        existing = conn.execute(
            "SELECT * FROM notice WHERE platform_code=? AND platform_notice_id=?",
            (platform, nid),
        ).fetchone()
        if existing and existing["content_hash"] == h:
            return "duplicated"

        project_id = _find_or_create_project(
            conn, project_code=doc.get("project_code"),
            region=doc.get("region"), title=doc["title"],
        )
        is_correction = "更正" in (doc.get("notice_type") or "")
        parent_id: int | None = None

        if existing:
            # 同平台同 id 但内容变化：原地更新 + 字段差异
            notice_id = existing["id"]
            parent_id = existing["parent_notice_id"]
            for field in TRACKED_FIELDS:
                _record_change(conn, project_id=project_id, notice_id=notice_id,
                               parent_id=parent_id, field=field,
                               old=existing[field], new=doc.get(field))
            conn.execute(
                "UPDATE notice SET title=?,region=?,budget=?,deadline=?,purchaser=?,"
                "content_hash=?,pdf_path=COALESCE(?,pdf_path),published_at=? WHERE id=?",
                (doc["title"], doc.get("region"), doc.get("budget"), doc.get("deadline"),
                 doc.get("purchaser"), h, doc.get("pdf_path"),
                 doc.get("published_at"), notice_id),
            )
            action = "corrected"
        else:
            if is_correction:
                # 挂接到同项目最早的原始招标公告
                parent = conn.execute(
                    "SELECT id FROM notice WHERE project_id=? AND notice_type != '更正公告' "
                    "ORDER BY published_at ASC, id ASC LIMIT 1", (project_id,),
                ).fetchone()
                parent_id = parent["id"] if parent else None
            cur = conn.execute(
                "INSERT INTO notice(project_id,platform_code,platform_name,"
                "platform_notice_id,notice_type,title,url,region,budget,deadline,"
                "purchaser,content_hash,pdf_path,published_at,parent_notice_id,crawled_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (project_id, platform, doc["platform_name"], nid,
                 doc.get("notice_type", "招标公告"), doc["title"], doc.get("url"),
                 doc.get("region"), doc.get("budget"), doc.get("deadline"),
                 doc.get("purchaser"), h, doc.get("pdf_path"),
                 doc.get("published_at"), parent_id, db.now_iso()),
            )
            notice_id = cur.lastrowid
            action = "corrected" if is_correction else "inserted"

            if is_correction and parent_id:
                # 更正后的字段值同步到项目下所有原始公告（跨平台转载的各份原文
                # 都应反映最新口径），每条同步逐字段留痕
                originals = conn.execute(
                    "SELECT * FROM notice WHERE project_id=? AND notice_type != '更正公告'",
                    (project_id,),
                ).fetchall()
                for parent in originals:
                    for field in TRACKED_FIELDS:
                        new_val = doc.get(field)
                        if new_val is not None:
                            _record_change(conn, project_id=project_id, notice_id=notice_id,
                                           parent_id=parent["id"], field=field,
                                           old=parent[field], new=new_val)
                    conn.execute(
                        "UPDATE notice SET title=?,region=?,"
                        "budget=COALESCE(?,budget),deadline=COALESCE(?,deadline),"
                        "purchaser=COALESCE(?,purchaser) WHERE id=?",
                        (doc["title"], doc.get("region"), doc.get("budget"),
                         doc.get("deadline"), doc.get("purchaser"), parent["id"]),
                    )
                log.info("更正公告 %s 已挂接原公告 #%s，并同步 %d 条原始公告（项目 #%s）",
                         nid, parent_id, len(originals), project_id)

        conn.execute(
            "UPDATE project SET title=?, region=COALESCE(?,region), "
            "latest_notice_id=?, updated_at=? WHERE id=?",
            (doc["title"], doc.get("region"), notice_id, db.now_iso(), project_id),
        )
        conn.commit()
    return action
