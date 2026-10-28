# -*- coding: utf-8 -*-
"""
主入口：运行这个就开始采集所有已登录的平台。

用法：  py main.py              → 跑所有已登录平台
       py main.py X            → 只跑X
       py main.py XHS          → 只跑小红书
       py main.py WX           → 只跑公众号
"""
import os
from browser_support import launch_browser
import sys
from playwright.sync_api import sync_playwright

import config
import crawler_x


def _state_dir(name):
    return os.path.join(config.BASE_DIR, name)


def _run_platform(p, state_path: str, name: str, crawler_func, require_login: bool):
    """启动一个平台的浏览器上下文，跑爬虫。"""
    if require_login:
        if not os.path.exists(state_path) or not os.listdir(state_path):
            print(f"[{name}] 还没登录！请先运行 py login_{name.lower().replace(' ','_')}.py")
            return
    try:
        headless = False
        context = launch_browser(p.chromium, persistent=True,
            user_data_dir=state_path,
            headless=headless,
            args=["--disable-blink-features=AutomationControlled"],
        )
        crawler_func(context)
        context.close()
    except Exception as e:
        print(f"[{name}] 采集出错: {e}")


def main():
    target = (sys.argv[1] if len(sys.argv) > 1 else "").upper()

    with sync_playwright() as p:
        # —— X ——
        if target in ("", "X"):
            _run_platform(p, _state_dir("browser_state"), "X", crawler_x.crawl, True)

        # —— 小红书 ——
        if target in ("", "XHS"):
            import crawler_xhs
            _run_platform(p, _state_dir("browser_state_xhs"), "小红书", crawler_xhs.crawl, True)

        # —— 微信公众号 ——
        if target in ("", "WX"):
            import crawler_wechat
            _run_platform(p, _state_dir("browser_state_wechat"), "公众号", crawler_wechat.crawl, False)

    # —— YouMind（不用浏览器，直接调API，需开VPN）——
    if target in ("", "YOUMIND", "YM"):
        try:
            import crawler_youmind
            crawler_youmind.crawl(pages=3)
        except Exception as e:
            print(f"[YouMind] 采集出错（确认VPN已开）: {e}")

    print("\n采集完成！运行 py server.py 打开网页查看。")


if __name__ == "__main__":
    main()
