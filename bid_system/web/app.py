"""Web 控制台 Flask 应用：任务/公告/变更/重试/设置 五个页面 + JSON API。"""
from __future__ import annotations

import io
import threading

import httpx
from flask import (Flask, jsonify, redirect, render_template, request,
                   send_file, url_for)
from openpyxl import Workbook

from .. import config, db
from ..collector import engine
from ..collector.scheduler import SCHEDULER
from ..utils import get_logger
from . import queries

log = get_logger("web")


def _mock_admin(method: str, path: str, **params) -> dict:
    """把控制台设置同步下发到三个 mock 平台的 admin 接口。"""
    result = {}
    for plat in config.PLATFORMS:
        try:
            with httpx.Client(timeout=10) as cli:
                resp = cli.request(method, f"{plat['base_url']}{path}",
                                   params=params)
                result[plat["code"]] = resp.status_code
        except httpx.HTTPError as exc:
            result[plat["code"]] = f"unreachable: {exc}"
    return result


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = config.SECRET_KEY

    # ------------------------------------------------------- 模板过滤器
    import json as _json

    @app.template_filter("from_json")
    def _from_json(value):
        try:
            return _json.loads(value or "[]")
        except (ValueError, TypeError):
            return []

    @app.template_filter("sum_dict")
    def _sum_dict(value):
        return sum(value.values()) if isinstance(value, dict) else 0

    @app.template_filter("dict_get")
    def _dict_get(mapping, key):
        return mapping.get(key) if isinstance(mapping, dict) else None

    from urllib.parse import urlencode

    @app.template_filter("urlencode_pairs")
    def _urlencode_pairs(value):
        return urlencode(value or {})

    # ----------------------------------------------------------- 页面
    @app.get("/")
    def index():
        return redirect(url_for("page_tasks"))

    @app.get("/tasks")
    def page_tasks():
        runs = queries.list_runs()
        settings = db.get_settings()
        with db.get_conn() as conn:
            pending_retry = conn.execute(
                "SELECT COUNT(*) AS c FROM retry_item WHERE status='pending'"
            ).fetchone()["c"]
            cp_rows = conn.execute("SELECT * FROM checkpoint").fetchall()
            mock_state = _safe_mock_state()
        return render_template(
            "tasks.html", runs=runs, settings=settings,
            pending_retry=pending_retry, checkpoints=[dict(r) for r in cp_rows],
            mock_state=mock_state, busy=engine.is_busy(),
            scheduler_running=SCHEDULER.running,
        )

    @app.get("/notices")
    def page_notices():
        rows, total, pages, clean = queries.list_notices(request.args)
        return render_template(
            "notices.html", notices=rows, total=total, pages=pages,
            page=max(1, request.args.get("page", 1, type=int)),
            regions=queries.list_regions(), clean=clean,
        )

    @app.get("/projects/<int:project_id>")
    def page_project(project_id: int):
        data = queries.project_detail(project_id)
        if not data:
            return "项目不存在", 404
        return render_template("project.html", **data)

    @app.get("/changes")
    def page_changes():
        page = max(1, request.args.get("page", 1, type=int))
        rows, pages = queries.list_changes(page)
        return render_template("changes.html", changes=rows, pages=pages, page=page)

    @app.get("/retries")
    def page_retries():
        status = request.args.get("status") or None
        items = engine.list_retry_items(status)
        return render_template("retries.html", items=items, current=status or "")

    @app.get("/settings")
    def page_settings():
        return render_template("settings.html", settings=db.get_settings(),
                               platforms=config.PLATFORMS,
                               mock_state=_safe_mock_state())

    # ----------------------------------------------------------- 任务 API
    @app.post("/api/tasks/run")
    def api_run():
        mode = request.form.get("mode", "incremental")
        platform = request.form.get("platform_code") or None
        if mode not in ("full", "incremental"):
            return jsonify({"ok": False, "error": "mode 必须是 full/incremental"}), 400
        if engine.is_busy():
            return jsonify({"ok": False, "error": "已有任务在执行"}), 409

        # 后台线程跑，避免 HTTP 请求长连接
        result: dict = {}

        def worker() -> None:
            try:
                result.update(engine.run_task(mode, platform, trigger="manual"))
            except Exception as exc:  # noqa: BLE001
                result.update({"ok": False, "error": str(exc)})

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=1.5)
        return jsonify({"ok": True, "started": True, "busy": engine.is_busy()})

    @app.get("/api/tasks/status")
    def api_status():
        return jsonify({"busy": engine.is_busy(),
                        "latest": queries.list_runs(1)[0] if queries.list_runs(1) else None})

    # ----------------------------------------------------------- 重试 API
    @app.post("/api/retries/<int:item_id>/rerun")
    def api_retry_one(item_id: int):
        return jsonify(engine.retry_one(item_id))

    @app.post("/api/retries/rerun-all")
    def api_retry_all():
        return jsonify(engine.retry_all_pending())

    # ----------------------------------------------------------- 设置 API
    @app.post("/api/settings/scheduler")
    def api_scheduler():
        enabled = request.form.get("enabled") == "1"
        cron = (request.form.get("schedule_cron") or "*/30 * * * *").strip()
        from ..collector.scheduler import cron_matches
        from datetime import datetime
        try:
            # 用一个固定时刻验证表达式可解析（不要求匹配成功）
            cron_matches(cron, datetime(2026, 1, 1, 10, 0))
        except ValueError:
            return jsonify({"ok": False, "error": "cron 表达式无法解析"}), 400
        db.save_setting("scheduler_enabled", "1" if enabled else "0")
        db.save_setting("schedule_cron", cron)
        return jsonify({"ok": True, "scheduler_enabled": enabled, "cron": cron})

    @app.post("/api/settings/mock/rate-limit")
    def api_rate_limit():
        limit = max(0, int(request.form.get("limit", 0)))
        db.save_setting("rate_limit_per_minute", limit)
        return jsonify({"ok": True, "synced": _mock_admin(
            "POST", "/admin/rate-limit", limit=limit)})

    @app.post("/api/settings/mock/flaky")
    def api_flaky():
        count = max(0, int(request.form.get("count", 0)))
        db.save_setting("flaky_fail_count", count)
        return jsonify({"ok": True, "synced": _mock_admin(
            "POST", "/admin/flaky", count=count)})

    @app.post("/api/settings/mock/expire-session")
    def api_expire():
        db.save_setting("auth_force_expire", "1")
        return jsonify({"ok": True, "synced": _mock_admin(
            "POST", "/admin/expire-sessions", force=1)})

    @app.post("/api/settings/mock/restore-session")
    def api_restore():
        db.save_setting("auth_force_expire", "0")
        return jsonify({"ok": True, "synced": _mock_admin(
            "POST", "/admin/expire-session", force=0)})

    @app.post("/api/mock/evolve/<int:round_no>")
    def api_evolve(round_no: int):
        return jsonify({"ok": True, "synced": _mock_admin(
            "POST", f"/admin/evolve/{round_no}")})

    @app.post("/api/mock/reset")
    def api_reset_mock():
        return jsonify({"ok": True, "synced": _mock_admin("POST", "/admin/reset")})

    # ----------------------------------------------------------- 导出
    @app.get("/export/excel")
    def export_excel():
        rows = queries.notices_for_export(request.args)
        wb = Workbook()
        ws = wb.active
        ws.title = "招投标公告"
        if rows:
            headers = list(rows[0].keys())
            ws.append(headers)
            for r in rows:
                ws.append([r[h] for h in headers])
            for i, h in enumerate(headers, start=1):
                ws.column_dimensions[ws.cell(row=1, column=i).column_letter].width = max(
                    12, min(40, len(str(h)) * 2 + 6))
        else:
            ws.append(["无符合筛选条件的数据"])
        buf = io.BytesIO()
        wb.save(buf)
        buf.seek(0)
        return send_file(
            buf, as_attachment=True, download_name="bidding_notices.xlsx",
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    return app


def _safe_mock_state() -> dict:
    """聚合三个 mock 平台的 admin/state，平台没起时给出不可达标记，不阻塞页面。"""
    state = {}
    for plat in config.PLATFORMS:
        try:
            with httpx.Client(timeout=3) as cli:
                resp = cli.get(f"{plat['base_url']}/admin/state")
                state[plat["code"]] = resp.json()
        except httpx.HTTPError as exc:
            state[plat["code"]] = {"unreachable": str(exc)}
    return state
