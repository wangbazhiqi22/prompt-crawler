# -*- coding: utf-8 -*-
"""
微信公众号爬虫（通过搜狗微信搜索）。
微信公众号没有公开搜索接口，唯一合法的公开入口是搜狗微信搜索。
用真实浏览器打开搜狗搜索页，从 DOM 提取文章卡片。
注意：搜狗反爬较强，偶尔弹验证码，遇到时需要手动在浏览器里点一下。
"""
import time
import datetime
import re
from urllib.parse import quote

import config
from database import get_conn, insert_item, count_items


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _check_captcha(page) -> bool:
    """检查是否弹了验证码。"""
    try:
        cap = page.locator("#seccodeImage, .seccode, [class*='captcha'], [id*='captcha']")
        if cap.count() > 0:
            return True
        # 搜狗特有：出现"请输入验证码"字样
        if "验证码" in (page.content() or ""):
            # 检查是否真的有验证码输入框
            inp = page.locator("input[name='verify']")
            if inp.count() > 0:
                return True
    except Exception:
        pass
    return False


def crawl(playwright_context):
    conn = get_conn()
    page = playwright_context.new_page()
    # 搜狗是 PC 页面
    page.set_viewport_size({"width": 1280, "height": 800})

    # 只搜精准词里的中文提示词相关
    wechat_keywords = [k for k in config.KEYWORDS_PRECISE if any("一" <= c <= "鿿" for c in k)][:5]
    if not wechat_keywords:
        wechat_keywords = ["AI提示词", "提示词 教程"][:3]

    total_new = 0
    for kw in wechat_keywords:
        print(f"\n[公众号] 搜索: {kw}")

        search_url = f"https://weixin.sogou.com/weixin?type=2&query={quote(kw)}"
        try:
            page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  打开失败: {e}")
            continue

        time.sleep(3)

        # 检查验证码
        if _check_captcha(page):
            print("  ⚠ 搜狗弹了验证码，请在弹出的浏览器里手动完成验证，脚本等待60秒…")
            time.sleep(60)
            # 验证后需要手动点搜索按钮或刷新
            try:
                page.locator("input[type='submit'], button[type='submit']").first.click(timeout=5000)
            except Exception:
                pass
            time.sleep(3)

        # 提取文章列表
        articles = page.locator(".news-list li, .news-list2 li, ul.news-list li, [class*='txt-box']").all()
        if not articles:
            articles = page.locator("ul[class*='news'] li, .wx-news-list li").all()

        print(f"  DOM 找到 {len(articles)} 个候选条目")

        new_kw = 0
        seen = set()
        for art in articles[:40]:  # 最多取前40条
            try:
                # 标题
                title = ""
                try:
                    t = art.locator("h3 a, .txt-box h3 a, h4 a, [class*='tit'] a").first
                    title = t.inner_text(timeout=2000).strip()
                    href = t.get_attribute("href", timeout=2000) or ""
                except Exception:
                    href = ""
                    try:
                        title = art.locator("h3, h4").first.inner_text(timeout=2000).strip()
                    except Exception:
                        pass

                # 摘要
                summary = ""
                try:
                    summary = art.locator(".txt-info, [class*='txt-info'], p[class*='txt']").first.inner_text(timeout=2000).strip()
                except Exception:
                    pass

                # 公众号名
                wx_name = ""
                try:
                    wx_name = art.locator(".account, [class*='account'], [class*='s-p'], .wx-name").first.inner_text(timeout=2000).strip()
                except Exception:
                    pass

                # 时间
                pub_time = ""
                try:
                    pub_time = art.locator(".s2, [class*='date'], [class*='time'], .s-p").last.inner_text(timeout=2000).strip()
                except Exception:
                    pass

                art_id = f"wx_{hash(title[:50] + wx_name)}"
                if art_id in seen:
                    continue
                seen.add(art_id)

                full_text = f"{title}\n{summary}"
                if config.PRE_FILTER_ENABLED:
                    from prompt_detector import looks_like_prompt
                    keep, score, reason = looks_like_prompt(full_text)
                    if not keep:
                        continue

                item = {
                    "platform": "微信公众号",
                    "platform_id": art_id,
                    "content_type": "文章",
                    "author_name": wx_name,
                    "author_id": wx_name,
                    "publish_time": pub_time,
                    "title": title,
                    "content": f"{title}\n\n{summary}",
                    "media_urls": "",
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                    "matched_keyword": kw,
                    "source_url": href,
                    "raw_json": "{}",
                    "crawled_at": _now_iso(),
                }
                if insert_item(conn, item):
                    new_kw += 1
            except Exception:
                continue

        total_new += new_kw
        print(f"  关键词[{kw}] 入库 {new_kw} 条")
        time.sleep(config.KEYWORD_INTERVAL_SECONDS + 3)  # 搜狗间隔久一点

    print(f"\n[公众号] 本轮共入库 {total_new} 条")
    conn.close()
    page.close()
