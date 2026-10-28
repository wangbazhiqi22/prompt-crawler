# -*- coding: utf-8 -*-
"""
首次登录脚本：只需运行一次。
打开浏览器窗口，你手动登录自己的 X 账号。
脚本会自动检测登录成功（检测到登录cookie），自动保存并关闭，无需手动按回车。
登录状态保存在 browser_state 目录，之后爬虫复用、免登录。
"""
import os
from browser_support import launch_browser
import time
from playwright.sync_api import sync_playwright
import config


def is_logged_in(context):
    """X 登录成功后会有 auth_token cookie。出错(窗口关了)返回 None。"""
    try:
        for c in context.cookies():
            if c.get("name") == "auth_token" and c.get("value"):
                return True
        return False
    except Exception:
        return None  # 浏览器/上下文已关闭


def main():
    os.makedirs(config.BROWSER_STATE_DIR, exist_ok=True)
    print("LOGIN_START", flush=True)
    print(">>> 浏览器即将打开，请在窗口里登录你的 X 账号。", flush=True)
    print(">>> 登录成功后本脚本会自动保存并关闭，请不要手动关闭浏览器窗口。", flush=True)

    with sync_playwright() as p:
        context = launch_browser(p.chromium, persistent=True,
            user_data_dir=config.BROWSER_STATE_DIR,
            headless=False,
            args=["--disable-blink-features=AutomationControlled"],
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto("https://x.com/login", timeout=60000)
        except Exception as e:
            print(f">>> 打开登录页出错（可能是网络/VPN）：{e}", flush=True)

        # 最多等 10 分钟，每 3 秒检测一次是否已登录
        deadline = time.time() + 10 * 60
        result = "TIMEOUT"
        while time.time() < deadline:
            status = is_logged_in(context)
            if status is True:
                result = "SUCCESS"
                break
            if status is None:
                result = "CLOSED"  # 窗口被关了
                break
            time.sleep(3)

        if result == "SUCCESS":
            time.sleep(2)  # 确保cookie落盘
            print("LOGIN_SUCCESS", flush=True)
            print(">>> 检测到登录成功，已保存登录态。", flush=True)
            try:
                context.close()
            except Exception:
                pass
        elif result == "CLOSED":
            print("LOGIN_CLOSED", flush=True)
            print(">>> 浏览器窗口被关闭了。如果你已经登录成功，登录态可能已保存；", flush=True)
            print(">>> 不确定的话请重新运行 py login.py，登录后不要手动关窗口。", flush=True)
        else:
            print("LOGIN_TIMEOUT", flush=True)
            print(">>> 超时未检测到登录。请重新运行 py login.py。", flush=True)
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
