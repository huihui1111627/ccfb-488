"""本地服务启动入口：三个 mock 平台 + Web 控制台。

用法：
    python -m bid_system.run_all            # 全部启动（mock :8101-8103 + 控制台 :8200）
    python -m bid_system.run_all --mock-only
    python -m bid_system.run_all --web-only
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bid_system import config, db
from bid_system.mock_platforms.servers import APP_FACTORIES
from bid_system.utils import get_logger

log = get_logger("runner")


def _run_app(app, port: int) -> None:
    # 关闭 reloader/调试器，线程内多 app 共用一个 werkzeug 进程
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False,
            threaded=True)


def start_mock_platforms() -> list[threading.Thread]:
    threads = []
    for code, (factory, port) in APP_FACTORIES.items():
        t = threading.Thread(target=_run_app, args=(factory(), port),
                             name=f"mock-{code}", daemon=True)
        t.start()
        threads.append(t)
        log.info("mock 平台 %s 监听 http://127.0.0.1:%s", code, port)
    return threads


def main() -> None:
    parser = argparse.ArgumentParser(description="招投标公告采集系统 本地启动")
    parser.add_argument("--mock-only", action="store_true")
    parser.add_argument("--web-only", action="store_true")
    args = parser.parse_args()

    db.init_db()

    if not args.web_only:
        start_mock_platforms()
        time.sleep(1.0)  # 等 werkzeug 起来
    if args.mock_only:
        log.info("--mock-only：仅运行 mock 平台，主线程保持驻留")
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            return

    from bid_system.web.app import create_app
    from bid_system.collector.scheduler import SCHEDULER

    SCHEDULER.start()
    app = create_app()
    log.info("Web 控制台监听 http://%s:%s", config.WEB_HOST, config.WEB_PORT)
    app.run(host=config.WEB_HOST, port=config.WEB_PORT, debug=False,
            use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
