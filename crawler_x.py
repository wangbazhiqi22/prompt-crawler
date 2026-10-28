# -*- coding: utf-8 -*-
"""
X (推特) 爬虫。
原理：用真实浏览器打开 X 搜索页，向下滚动，
拦截 X 后台返回的 SearchTimeline JSON 接口，从中解析出每条推文。
不解析网页HTML（那个易变），只吃干净的数据接口。
"""
import json
import time
import datetime
import re

from playwright.sync_api import sync_playwright

import config
from database import get_conn, insert_item, count_items


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _is_chinese(text: str) -> bool:
    if not text:
        return False
    return bool(re.search(r"[一-鿿]", text))


def _parse_twitter_time(s: str) -> str:
    """X 的时间格式如 'Wed Oct 10 20:19:24 +0000 2018' -> ISO 字符串。"""
    if not s:
        return ""
    try:
        dt = datetime.datetime.strptime(s, "%a %b %d %H:%M:%S %z %Y")
        return dt.isoformat()
    except Exception:
        return s


def _extract_tweets_from_json(data: dict):
    """
    从 X 的 SearchTimeline JSON 里把每条推文挖出来。
    X 的返回结构层层嵌套，这里递归找到所有 'tweet_results' 节点。
    """
    results = []

    def walk(node):
        if isinstance(node, dict):
            # 推文结果通常在 result 里，且有 legacy 字段
            if node.get("__typename") == "Tweet" and "legacy" in node:
                results.append(node)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return results


def _tweet_to_item(tweet: dict, keyword: str) -> dict:
    """把一条 X 推文对象整理成我们统一的数据结构。"""
    legacy = tweet.get("legacy", {}) or {}

    # 作者信息
    user = (
        tweet.get("core", {})
        .get("user_results", {})
        .get("result", {})
    )
    user_legacy = user.get("legacy", {}) or {}
    author_name = user_legacy.get("name", "") or user_legacy.get("screen_name", "")
    author_id = user_legacy.get("screen_name", "")

    # 正文：优先用 note_tweet（长推全文），否则用 full_text
    full_text = legacy.get("full_text", "") or ""
    note = (
        tweet.get("note_tweet", {})
        .get("note_tweet_results", {})
        .get("result", {})
        .get("text")
    )
    content = note or full_text

    # 媒体链接
    media_urls = []
    entities = legacy.get("extended_entities", {}) or legacy.get("entities", {}) or {}
    for m in entities.get("media", []) or []:
        url = m.get("media_url_https") or m.get("expanded_url")
        if url:
            media_urls.append(url)

    tweet_id = legacy.get("id_str") or tweet.get("rest_id", "")
    source_url = (
        f"https://x.com/{author_id}/status/{tweet_id}" if author_id and tweet_id else ""
    )

    return {
        "platform": "X",
        "platform_id": tweet_id,
        "content_type": "帖子",
        "author_name": author_name,
        "author_id": author_id,
        "publish_time": _parse_twitter_time(legacy.get("created_at", "")),
        "title": "",
        "content": content,
        "media_urls": " ".join(media_urls),
        "like_count": legacy.get("favorite_count", 0),
        "comment_count": legacy.get("reply_count", 0),
        "share_count": legacy.get("retweet_count", 0),
        "matched_keyword": keyword,
        "source_url": source_url,
        "raw_json": json.dumps(tweet, ensure_ascii=False),
        "crawled_at": _now_iso(),
    }


def crawl(playwright_context):
    """主流程：对每个关键词搜索 + 滚动 + 拦截 + 入库。"""
    conn = get_conn()
    page = playwright_context.new_page()

    # 用一个容器收集本次拦截到的所有推文（按关键词）
    captured = {"tweets": []}

    def handle_response(response):
        url = response.url
        if "SearchTimeline" in url or "adaptive.json" in url:
            try:
                data = response.json()
            except Exception:
                return
            tweets = _extract_tweets_from_json(data)
            captured["tweets"].extend(tweets)

    page.on("response", handle_response)

    total_new = 0
    for kw in config.KEYWORDS:
        captured["tweets"] = []
        print(f"\n[X] 搜索关键词: {kw}")

        # 打开搜索页（live=最新）
        from urllib.parse import quote
        search_url = (
            f"https://x.com/search?q={quote(kw)}&src=typed_query&f={config.X_SEARCH_TAB}"
        )
        try:
            page.goto(search_url, timeout=30000)
        except Exception as e:
            print(f"  打开页面失败: {e}")
            continue

        time.sleep(config.SCROLL_WAIT_SECONDS)

        # 向下滚动若干次，触发更多数据加载
        for i in range(config.SCROLL_TIMES):
            page.mouse.wheel(0, 3000)
            time.sleep(config.SCROLL_WAIT_SECONDS)
            print(f"  滚动 {i + 1}/{config.SCROLL_TIMES}，已捕获 {len(captured['tweets'])} 条原始数据")

        # 入库过滤逻辑：
        #  ① 所有词：先过基础垃圾检查（纯链接/广告等当场丢弃）
        #  ② 精准词：跳过提示词结构检测，直接入库
        #  ③ 宽泛词：必须过提示词结构检测，不通过则丢弃
        new_this_kw = 0
        filtered_kw = 0
        seen = set()
        is_precise = kw in getattr(config, "PRECISE_KEYWORDS", set())

        for tw in captured["tweets"]:
            item = _tweet_to_item(tw, kw)
            if not item["platform_id"] or item["platform_id"] in seen:
                continue
            seen.add(item["platform_id"])
            if config.ONLY_CHINESE and not _is_chinese(item["content"]):
                continue

            if getattr(config, "PRE_FILTER_ENABLED", True):
                from prompt_detector import is_junk, looks_like_prompt
                # ① 基础垃圾检查（所有词都过）
                if is_junk(item["content"]):
                    filtered_kw += 1
                    continue
                # ② 宽泛词额外过提示词结构检测
                if not is_precise:
                    keep, score, reason = looks_like_prompt(item["content"])
                    if not keep:
                        filtered_kw += 1
                        continue

            if insert_item(conn, item):
                new_this_kw += 1
        total_new += new_this_kw
        tag = "精准" if is_precise else "宽泛(过滤)"
        print(f"  关键词[{kw}] {tag} → 入库 {new_this_kw}，过滤 {filtered_kw}")

        time.sleep(config.KEYWORD_INTERVAL_SECONDS)

    print(f"\n[X] 本轮共新入库 {total_new} 条，数据库 X 平台累计 {count_items(conn, 'X')} 条")
    conn.close()
    page.close()
