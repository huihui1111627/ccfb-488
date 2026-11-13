"""三个 mock 招标平台的 Flask 应用。

平台一 html_gov :8101  纯 HTML 列表分页 + 详情页 + PDF 附件
平台二 json_ggzy:8102  JSON 分页接口 + JSON 详情 + PDF 附件
平台三 auth_jy  :8103  登录页(JS 生成 CSRF 字段) + Cookie 会话(30 分钟) + JSON 数据

预算与截止时间只出现在 PDF 附件里，列表/详情接口刻意不给，强制采集端走附件解析链路。
"""
from __future__ import annotations

from flask import Flask, Response, abort, jsonify, redirect, render_template_string, request

from .. import config
from ..utils import get_logger
from .store import PAGE_SIZE, STORE

log = get_logger("mock")

# --------------------------------------------------------------------- 公共控制
def _guard(platform: str) -> Response | None:
    retry_after = STORE.check_rate(platform, request.remote_addr or "local")
    if retry_after is not None:
        resp = jsonify({"code": 429, "error": "rate_limited",
                        "message": f"请求过于频繁，请 {retry_after} 秒后重试"})
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp
    return None


def _add_admin_routes(app: Flask, platform: str) -> None:
    @app.get("/admin/state")
    def admin_state():
        return jsonify(STORE.state())

    @app.post("/admin/rate-limit")
    def admin_rate():
        limit = int(request.args.get("limit", request.form.get("limit", 0)))
        STORE.set_rate_limit(platform, max(0, limit))
        return jsonify({"ok": True, "platform": platform, "limit_per_min": limit})

    @app.post("/admin/flaky")
    def admin_flaky():
        count = int(request.args.get("count", request.form.get("count", 0)))
        STORE.set_flaky(platform, max(0, count))
        return jsonify({"ok": True, "platform": platform, "flaky_remaining": count})

    @app.post("/admin/evolve/<int:round_no>")
    def admin_evolve(round_no: int):
        return jsonify({"ok": True, "added": STORE.evolve(round_no)})

    @app.post("/admin/reset")
    def admin_reset():
        STORE.seed()
        STORE.drop_sessions()
        return jsonify({"ok": True, "state": STORE.state()})


def _pdf_response(platform: str, nid: str) -> Response:
    data = STORE.build_pdf(platform, nid)
    if data is None:
        abort(404)
    return Response(data, mimetype="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{nid}.pdf"'})


# ===================================================================== 平台一
def create_html_app() -> Flask:
    platform = "html_gov"
    app = Flask(f"mock_{platform}")
    _add_admin_routes(app, platform)

    LIST_TMPL = """
    <!doctype html><html lang="zh"><head><meta charset="utf-8">
    <title>华信市政府采购网 - 公告列表</title></head><body>
    <h2>华信市政府采购网 招标公告（第 {{ page }} / {{ total_pages }} 页）</h2>
    <table border="1" cellpadding="6" cellspacing="0">
      <tr><th>公告编号</th><th>标题</th><th>类型</th><th>地区</th><th>发布时间</th></tr>
      {% for n in items %}
      <tr class="notice-row">
        <td>{{ n.id }}</td>
        <td><a class="notice-link" href="/notice/{{ n.id }}">{{ n.title }}</a></td>
        <td>{{ n.type }}</td><td>{{ n.region }}</td><td>{{ n.published_at }}</td>
      </tr>
      {% endfor %}
    </table>
    <div class="pager">
      {% if page > 1 %}<a href="/notices?page={{ page-1 }}">上一页</a>{% endif %}
      <span>第 {{ page }} 页</span>
      {% if page < total_pages %}<a href="/notices?page={{ page+1 }}">下一页</a>{% endif %}
      <span class="total">共 {{ total }} 条</span>
    </div></body></html>
    """

    DETAIL_TMPL = """
    <!doctype html><html lang="zh"><head><meta charset="utf-8"><title>{{ n.title }}</title></head>
    <body>
    <h2 id="notice-title">{{ n.title }}</h2>
    <ul id="notice-meta">
      <li>公告编号：<span id="nid">{{ n.id }}</span></li>
      <li>项目编号：<span id="project-code">{{ n.project_code }}</span></li>
      <li>类型：<span id="notice-type">{{ n.type }}</span></li>
      <li>地区：<span id="region">{{ n.region }}</span></li>
      <li>采购人：<span id="purchaser">{{ n.purchaser }}</span></li>
      <li>发布时间：<span id="published-at">{{ n.published_at }}</span></li>
    </ul>
    <p id="content">{{ n.content }}</p>
    <p>预算金额与投标截止时间详见附件招标文件：
       <a id="attachment" href="/attachment/{{ n.id }}">下载附件 PDF</a></p>
    </body></html>
    """

    @app.get("/")
    def index():
        return redirect("/notices?page=1")

    @app.get("/notices")
    def notices():
        limited = _guard(platform)
        if limited:
            return limited
        page = max(1, request.args.get("page", 1, type=int))
        items, total = STORE.list_notices(platform, page)
        total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        return render_template_string(
            LIST_TMPL, items=items, page=page, total=total, total_pages=total_pages)

    @app.get("/notice/<nid>")
    def detail(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        if STORE.consume_flaky(platform):
            abort(500, description="平台详情服务暂时不可用(故障注入)")
        n = STORE.get_notice(platform, nid)
        if not n:
            abort(404)
        return render_template_string(DETAIL_TMPL, n=n)

    @app.get("/attachment/<nid>")
    def attachment(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        return _pdf_response(platform, nid)

    return app


# ===================================================================== 平台二
def create_json_app() -> Flask:
    platform = "json_ggzy"
    app = Flask(f"mock_{platform}")
    _add_admin_routes(app, platform)

    def ok(data):
        return jsonify({"code": 0, "message": "ok", "data": data})

    @app.get("/")
    def index():
        return ok({"service": "省公共资源交易中心开放接口", "docs": "/api/notices?page=1"})

    @app.get("/api/notices")
    def api_list():
        limited = _guard(platform)
        if limited:
            return limited
        page = max(1, request.args.get("page", 1, type=int))
        page_size = request.args.get("page_size", PAGE_SIZE, type=int)
        items, total = STORE.list_notices(platform, page)
        summaries = [{k: n[k] for k in
                      ("id", "project_code", "title", "type", "region", "published_at")}
                     for n in items]
        return ok({"total": total, "page": page, "page_size": page_size, "items": summaries})

    @app.get("/api/notice/<nid>")
    def api_detail(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        if STORE.consume_flaky(platform):
            resp = jsonify({"code": 500, "message": "详情服务内部错误(故障注入)"})
            resp.status_code = 500
            return resp
        n = STORE.get_notice(platform, nid)
        if not n:
            return jsonify({"code": 404, "message": "公告不存在"}), 404
        n = dict(n)
        n["attachment_url"] = f"/attachment/{nid}"
        return ok(n)

    @app.get("/attachment/<nid>")
    def attachment(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        return _pdf_response(platform, nid)

    return app


# ===================================================================== 平台三
LOGIN_PAGE = """
<!doctype html><html lang="zh"><head><meta charset="utf-8"><title>重点工程交易平台 - 登录</title></head>
<body>
<h2>重点工程交易平台（登录后访问，会话 30 分钟有效）</h2>
<form id="login-form" method="post" action="/login">
  <input type="hidden" id="csrft" name="csrft" value="">
  <p>账号 <input name="username" id="username" autocomplete="off"></p>
  <p>密码 <input name="password" id="password" type="password"></p>
  <button type="submit">登录</button>
</form>
<p id="tip" style="color:#888">页面脚本会生成一次性校验字段</p>
<script>
// 前端动态生成 CSRF 字段：纯 httpx 拿不到，需要浏览器执行 JS（或逆向该常量）
document.getElementById('csrft').value = 'tok_' + (7 * 191);
</script>
</body></html>
"""


def create_auth_app() -> Flask:
    platform = "auth_jy"
    app = Flask(f"mock_{platform}")
    _add_admin_routes(app, platform)

    def require_session():
        token = request.cookies.get(config.AUTH_COOKIE_NAME)
        if not STORE.check_session(token):
            resp = jsonify({"code": 401, "error": "session_expired",
                            "message": "登录已过期，请重新登录"})
            resp.status_code = 401
            return None, resp
        return token, None

    @app.get("/")
    def index():
        token, err = require_session()
        if err:
            return redirect("/login")
        return jsonify({"code": 0, "message": f"会话有效 {token[:8]}..."})

    @app.get("/login")
    def login_page():
        return render_template_string(LOGIN_PAGE)

    @app.post("/login")
    def login_submit():
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        csrft = request.form.get("csrft", "")
        # 校验 JS 生成字段，证明浏览器执行过脚本
        if csrft != "tok_1337":
            return "校验字段无效，请启用 JavaScript", 400
        if username != config.AUTH_USERNAME or password != config.AUTH_PASSWORD:
            return "账号或密码错误", 401
        token = STORE.login(username, password)
        resp = redirect("/")
        resp.set_cookie(config.AUTH_COOKIE_NAME, token,
                        max_age=config.SESSION_TTL_MINUTES * 60, httponly=True)
        log.info("auth 平台登录成功，会话 30 分钟有效")
        return resp

    @app.post("/admin/expire-sessions")
    def admin_expire():
        STORE.force_expire = bool(int(request.args.get("force", 1)))
        if STORE.force_expire:
            STORE.drop_sessions()
        return jsonify({"ok": True, "force_expire": STORE.force_expire, "sessions": 0})

    @app.get("/api/notices")
    def api_list():
        limited = _guard(platform)
        if limited:
            return limited
        _, err = require_session()
        if err:
            return err
        page = max(1, request.args.get("page", 1, type=int))
        items, total = STORE.list_notices(platform, page)
        summaries = [{k: n[k] for k in
                      ("id", "project_code", "title", "type", "region", "published_at")}
                     for n in items]
        return jsonify({"code": 0, "data":
                        {"total": total, "page": page, "page_size": PAGE_SIZE,
                         "items": summaries}})

    @app.get("/api/notice/<nid>")
    def api_detail(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        _, err = require_session()
        if err:
            return err
        if STORE.consume_flaky(platform):
            resp = jsonify({"code": 500, "message": "详情服务内部错误(故障注入)"})
            resp.status_code = 500
            return resp
        n = STORE.get_notice(platform, nid)
        if not n:
            return jsonify({"code": 404, "message": "公告不存在"}), 404
        n = dict(n)
        n["attachment_url"] = f"/attachment/{nid}"
        return jsonify({"code": 0, "data": n})

    @app.get("/attachment/<nid>")
    def attachment(nid: str):
        limited = _guard(platform)
        if limited:
            return limited
        _, err = require_session()
        if err:
            return err
        return _pdf_response(platform, nid)

    return app


APP_FACTORIES = {
    "html_gov": (create_html_app, 8101),
    "json_ggzy": (create_json_app, 8102),
    "auth_jy": (create_auth_app, 8103),
}
