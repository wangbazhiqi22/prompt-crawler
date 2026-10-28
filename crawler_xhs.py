# -*- coding: utf-8 -*-
"""
小红书爬虫。
用真实浏览器打开小红书搜索页，向下滚动，从渲染好的 DOM 里提取笔记卡片。
小红书反爬极强，走 DOM 提取比拦截 API 更稳定（浏览器自己处理签名）。
"""
import time
import datetime
import re
import json
from urllib.parse import quote

import config
from database import get_conn, insert_item, count_items


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


# 进每篇笔记详情页抓正文的篇数上限（试水阶段设小，防风控）
DETAIL_FETCH_LIMIT = 8


def _fetch_detail(page, url: str) -> dict:
    """点进一篇笔记详情页，抓完整正文 + 图片。失败返回空 dict。"""
    try:
        page.goto(url, timeout=25000, wait_until="domcontentloaded")
    except Exception:
        return {}
    time.sleep(3)  # 等正文渲染

    detail = {"content": "", "media": [], "title": "", "publish_time": ""}

    # 正文：实测小红书详情页正文在 span.note-text（最准）
    for sel in ["span.note-text", "#detail-desc", ".note-text", ".note-scroller"]:
        try:
            txt = page.locator(sel).first.inner_text(timeout=3000).strip()
            if txt and len(txt) > len(detail["content"]):
                detail["content"] = txt
        except Exception:
            continue

    # 标题
    for sel in ["#detail-title", ".title", "[class*='title']"]:
        try:
            t = page.locator(sel).first.inner_text(timeout=2000).strip()
            if t:
                detail["title"] = t
                break
        except Exception:
            continue

    # 图片（笔记配图，含提示词截图常在这里）
    try:
        imgs = page.locator("img[src*='xhscdn'], .swiper-slide img, [class*='img'] img").all()
        for im in imgs[:9]:
            src = im.get_attribute("src", timeout=1500)
            if src and "xhscdn" in src and src not in detail["media"]:
                detail["media"].append(src)
    except Exception:
        pass

    return detail


def crawl(playwright_context):
    conn = get_conn()
    page = playwright_context.new_page()
    # 手机尺寸，小红书匹配移动端
    page.set_viewport_size({"width": 430, "height": 900})

    total_new = 0
    # 小红书是中文社区，只用中文关键词（英文词搜不到结果）。
    # 针对短剧/AI视频业务挑选高命中词。
    xhs_keywords = getattr(config, "XHS_KEYWORDS", None) or [
        "即梦 提示词",
        "可灵 提示词",
        "即梦 运镜 提示词",
        "AI视频 提示词",
        "ChatGPT 提示词",
        "AI绘画 提示词",
        "Midjourney 提示词",
        "分镜 提示词",
    ]

    for kw in xhs_keywords:
        print(f"\n[XHS] 搜索关键词: {kw}")

        search_url = f"https://www.xiaohongshu.com/search_result?keyword={quote(kw)}&source=web_search_result_notes"
        try:
            page.goto(search_url, timeout=30000, wait_until="domcontentloaded")
        except Exception as e:
            print(f"  打开失败: {e}")
            continue

        time.sleep(config.SCROLL_WAIT_SECONDS + 1)

        # 滚动加载
        for i in range(config.SCROLL_TIMES + 2):  # XHS 加载慢一点，多滚几次
            page.mouse.wheel(0, 2500)
            time.sleep(config.SCROLL_WAIT_SECONDS)
            if i % 3 == 0:
                print(f"  滚动 {i+1}/{config.SCROLL_TIMES+2}")

        # 用封面图作为点击入口（实测：点 a.cover 才能用JS打开带token的详情页）
        covers = page.locator("a.cover")
        cover_count = covers.count()
        print(f"  找到 {cover_count} 个笔记封面，准备逐个点开抓正文（最多 {DETAIL_FETCH_LIMIT} 篇）")

        # —— 逐个点封面 → 抓正文 → Esc 返回 ——
        new_kw = 0
        seen = set()
        n_open = min(cover_count, DETAIL_FETCH_LIMIT)
        for idx in range(n_open):
            try:
                # 每次重新定位（DOM 可能刷新）
                cover = page.locator("a.cover").nth(idx)
                cover.scroll_into_view_if_needed(timeout=3000)

                # 点击前，先从列表卡片本身抓封面图（实测卡片第一个img即封面，
                # sns-webpic-qc 域名；不要 author-avatar 头像）
                card_cover_img = ""
                try:
                    note_card = page.locator("section.note-item").nth(idx)
                    card_imgs = note_card.locator("img").all()
                    for im in card_imgs:
                        src = im.get_attribute("src", timeout=1000) or ""
                        cls = im.get_attribute("class", timeout=500) or ""
                        if "avatar" in cls or "avatar" in src:
                            continue  # 跳过头像
                        if "xhscdn" in src:
                            card_cover_img = src
                            break
                except Exception:
                    pass

                cover.click(timeout=4000)
                time.sleep(3)  # 等详情浮层加载

                # 当前 URL 即真实笔记链接（带 token）
                cur_url = page.url
                note_id = ""
                m = re.search(r"explore/([a-f0-9]+)", cur_url)
                if m:
                    note_id = m.group(1)
                if not note_id:
                    note_id = f"xhs_{idx}_{abs(hash(cur_url))}"

                # 抓正文
                content = ""
                for sel in ["span.note-text", "#detail-desc", ".note-text", ".note-scroller"]:
                    try:
                        t = page.locator(sel).first.inner_text(timeout=2500).strip()
                        if t and len(t) > len(content):
                            content = t
                    except Exception:
                        continue

                # 抓标题
                title = ""
                for sel in ["#detail-title", ".title", "[class*='title']"]:
                    try:
                        title = page.locator(sel).first.inner_text(timeout=1500).strip()
                        if title:
                            break
                    except Exception:
                        continue

                # 抓作者
                author = ""
                for sel in [".username", "[class*='username']", ".author-wrapper .name"]:
                    try:
                        author = page.locator(sel).first.inner_text(timeout=1500).strip()
                        if author:
                            break
                    except Exception:
                        continue

                # 抓图片：封面图优先用列表卡片那张（最准）
                urls = []
                if card_cover_img:
                    # 补全协议头
                    cov = card_cover_img if card_cover_img.startswith("http") else "https:" + card_cover_img
                    urls.append(cov)
                # 详情页轮播图作为补充（排除头像和公共logo）
                try:
                    imgs = page.locator(".swiper-slide img, .note-slider-img").all()
                    for im in imgs[:9]:
                        src = im.get_attribute("src", timeout=1000) or ""
                        if not src or "avatar" in src or "fe-platform" in src:
                            continue  # 跳过头像和平台logo
                        if "xhscdn" in src:
                            full = src if src.startswith("http") else "https:" + src
                            if full not in urls:
                                urls.append(full)
                except Exception:
                    pass
                media = " ".join(urls)

                # 返回搜索结果页
                page.keyboard.press("Escape")
                time.sleep(1.5)

                if note_id in seen:
                    continue
                seen.add(note_id)

                full_content = (title + "\n\n" + content).strip() if content else title
                if not full_content or len(full_content) < 5:
                    continue

                # 过结构检测（含正文，判断准）
                if config.PRE_FILTER_ENABLED:
                    from prompt_detector import looks_like_prompt
                    keep, score, reason = looks_like_prompt(full_content)
                    if not keep:
                        continue
                content = full_content

                item = {
                    "platform": "小红书",
                    "platform_id": note_id,
                    "content_type": "笔记",
                    "author_name": author,
                    "author_id": "",
                    "publish_time": _now_iso(),
                    "title": title,
                    "content": content,
                    "media_urls": media,
                    "like_count": 0,
                    "comment_count": 0,
                    "share_count": 0,
                    "matched_keyword": kw,
                    "source_url": cur_url,
                    "raw_json": "{}",
                    "crawled_at": _now_iso(),
                }
                if insert_item(conn, item):
                    new_kw += 1
            except Exception:
                # 单篇出错（点击/加载失败），尝试Esc回到列表继续
                try:
                    page.keyboard.press("Escape")
                    time.sleep(1)
                except Exception:
                    pass
                continue

        total_new += new_kw
        print(f"  关键词[{kw}] 入库 {new_kw} 条")
        time.sleep(config.KEYWORD_INTERVAL_SECONDS)

    print(f"\n[XHS] 本轮共入库 {total_new} 条，数据库小红书累计 {count_items(conn, '小红书')} 条")
    conn.close()
    page.close()
