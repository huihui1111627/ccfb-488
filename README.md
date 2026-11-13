# 招投标公告采集系统

面向工程咨询场景的多平台招投标公告采集与变更跟踪系统。内置三个可演化的本地 mock
招标平台，覆盖纯 HTML 分页、JSON 接口、登录会话（30 分钟过期）三种真实形态，
配 Web 控制台完成任务编排、公告检索、变更追溯、失败重试与系统设置。

## 技术栈

| 层 | 选型 |
|---|---|
| 采集 | httpx（列表/详情/PDF，自动退避重试）+ Playwright（登录平台执行 JS）+ BeautifulSoup4 |
| 存储 | SQLite（WAL 模式，唯一键 + checkpoint 保证幂等续跑） |
| PDF | reportlab 生成中文公告附件（mock 侧）/ pypdf 抽取预算与截止时间（采集侧） |
| Web | Flask + 原生 Jinja 模板，openpyxl 导出 Excel |
| 调度 | 内置轻量 cron 调度线程（无额外依赖） |

## 快速开始

```bash
pip install -r requirements.txt
python -m playwright install chromium      # 已安装可跳过

# 方式一：一键启动（3 个 mock 平台 :8101-8103 + 控制台 :8200）
python bid_system/run_all.py
# 浏览器打开 http://127.0.0.1:8200

# 方式二：不带 Web 的端到端自检（38 项断言，约 2 分钟，含限流退避等待）
python bid_system/demo.py

# 方式三：纯函数单元测试
python bid_system/tests/test_utils.py
```

> 所有 Python 入口均支持隔离模式 `python -I`，脚本内部已处理模块搜索路径。

## 控制台五个页面

1. **采集任务** — 手动全量/增量（可按平台）、引擎状态、mock 平台实时状态、
   checkpoint 断点、任务历史（新增/更正/重复/失败计数）。
2. **公告列表** — 地区、金额区间、关键词、公告类型组合筛选，分页，一键导出当前筛选 Excel；
   点标题进入项目详情（跨平台转载归并视图 + 字段变更时间线）。
3. **变更记录** — 更正公告与原公告的字段级差异（原值 → 新值），支持多次更正完整追溯。
4. **失败重试** — 失败条目按阶段/原因入队，可单条手动重跑或一键重跑，超次数标记 dead。
5. **设置** — 定时调度开关与 cron 表达式；mock 故障注入（限流/500/会话过期）；
   两轮数据演化控制与平台连接信息。

## 推荐演示路径（两轮增量看新增与更正）

1. 启动后在「采集任务」页点 **全量重采**：16 条公告入库，跨平台转载去重为 10 个项目。
2. 再点一次全量：16 条全部 `duplicated`，**零重复入库**。
3. 到「设置」页点 **演化第 1 轮**，回任务页点 **手动增量采集**：
   只处理 5 条新内容（3 新增 + 2 更正），「变更记录」出现预算/截止时间差异。
4. 点 **演化第 2 轮** 后再增量：含同一项目的二次更正，差异全部可追溯。
5. 故障演练（设置页）：
   - 限流阈值设小 → 采集时观察日志按 `Retry-After` 退避后自动恢复；
   - 故障注入次数 > 4 → 退避用尽的条目进重试队列，到「失败重试」页手动重跑；
   - 强制会话过期 → auth 平台采集时自动 Playwright 续登，任务不中断。

## 目录结构

```
bid_system/
├── config.py                 # 平台地址、凭据、退避序列等配置
├── db.py                     # SQLite 建表/DAO/settings/checkpoint
├── utils.py                  # 去重指纹、金额与日期解析、日志
├── run_all.py                # 服务启动入口
├── demo.py                   # 端到端自检脚本（38 断言）
├── mock_platforms/
│   ├── store.py              # 共享数据仓库：两轮演化、限流、故障、登录会话
│   ├── servers.py            # 三个 Flask mock 平台（HTML/JSON/登录）
│   └── pdf_doc.py            # 中文公告 PDF 生成
├── collector/
│   ├── http_client.py        # 429/5xx 指数退避、Retry-After、401 上抛
│   ├── browser.py            # Playwright 登录（httpx 逆向常量回退）
│   ├── fetchers.py           # 三种平台适配器：分页→详情→PDF→标准文档
│   ├── pdf_parse.py          # PDF 文本抽取与字段解析
│   ├── repo.py               # 去重落库、更正挂接、字段差异留痕
│   ├── engine.py             # 任务编排、断点续跑、重试队列
│   └── scheduler.py          # cron 定时调度线程
├── web/
│   ├── app.py                # Flask 路由与 JSON API
│   ├── queries.py            # 筛选/分页/导出查询
│   ├── templates/            # 5 个页面 + 项目详情
│   └── static/style.css
└── tests/test_utils.py       # 14 个纯函数单测
```

## 关键设计说明

### 去重（两层）
- **项目层** `project.project_key`：优先用归一化项目编号；编号缺失时退化为
  「地区 + 标题指纹（去标点/平台前后缀噪声）」的 SHA1。多平台转载归并到同一项目。
- **公告层** `notice UNIQUE(platform_code, platform_notice_id)` + 内容哈希：
  同平台同公告重复采集直接判 `duplicated`，内容变化才更新并留痕。

### 更正公告
- 识别到 `更正公告` 类型后挂接到同项目最早的原始公告（`parent_notice_id`）。
- 更正字段同步到项目下**所有平台**的原始公告，保证各份原文口径一致。
- 每个字段的旧值/新值写入 `field_change`，多次更正形成完整时间线，原数据不覆盖丢失。

### 断点续跑与幂等
- `checkpoint` 记录每平台列表页码、增量水位（最大发布时间）与已见条目 id 集合，
  每条公告处理成功后立即推进。
- 增量按页判断（整页均为历史条目才停止），新公告排在升序列表尾部也不会漏。
- 崩溃重启后靠唯一键 + 水位继续，已处理条目不会重复入库。

### 退避、续登与重试
- 429 读 `Retry-After`（封顶 30s），5xx 走 2/5/10/20s 指数退避，共 4 次；
  连接级异常同样重试。
- auth 平台 401 触发 `AuthExpiredError` → Playwright 重新执行 JS 登录 → 原样重试；
  浏览器不可用时回退到已逆向的 CSRF 常量直登。
- 退避用尽仍失败的单条进入 `retry_item`（同条同阶段去重），任务整体标记 `partial`
  不中断；手动重跑超 50 次标记 `dead`。

### PDF 字段抽取
mock 公告的预算与截止时间**只出现在 PDF 附件里**（列表/详情接口刻意不提供），
强制走完整附件链路。pypdf 抽取后用中文正则识别「预算金额/最高限价/万元换算」
与「截止/递交/开标」附近的日期时间。

## 接真实平台时的改造点

- 在 `config.PLATFORMS` 增加平台定义，在 `fetchers.py` 按 HTML/JSON/登录三种基类派生适配器；
- 登录字段逆向结果放 `browser.py`，凭据改用环境变量或 settings 加密存储；
- 调度目前是单进程线程模型，多实例部署时把 `engine._run_lock` 换成数据库行锁或分布式锁；
- SQLite 换 PostgreSQL 只需替换 `db.get_conn` 与少量占位符语法。
