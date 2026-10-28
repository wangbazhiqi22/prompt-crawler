# -*- coding: utf-8 -*-
"""Start the original Flask server with optional runtime extensions loaded."""

import os
import socket

import sitecustomize  # noqa: F401 - applies runtime extractor extensions.


ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)

import server  # noqa: E402
from auth_manager import register_auth  # noqa: E402
from crawler_bilibili import register_routes  # noqa: E402


register_auth(server.app)
register_routes(server)

try:
    from crawler_douyin import reset_stuck_processing

    n = reset_stuck_processing()
    if n:
        print(f"  已清理 {n} 条中断的抖音解析任务（可重新解析）")
except Exception:
    pass

try:
    from crawler_bilibili import reset_stuck_processing as reset_bili_stuck_processing

    n = reset_bili_stuck_processing()
    if n:
        print(f"  已清理 {n} 条超时的 B站解析任务（可重新解析）")
except Exception:
    pass


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    from waitress import serve
    host = os.environ.get("PROMPT_CRAWLER_HOST", "127.0.0.1")
    port = int(os.environ.get("PROMPT_CRAWLER_PORT", "5000"))
    print(f"Prompt Crawler: http://{host}:{port}")
    print("First login: read the random password in data/first-run-admin.txt.")
    serve(server.app, host=host, port=port, threads=8)


if __name__ == "__main__":
    main()
