# -*- coding: utf-8 -*-
"""
YouMind 采集模块（直接调官方 API，质量最高的数据源）。
youmind.com 是专门的 AI 提示词素材平台，有 1.4 万+ 条已整理好的精品提示词，
每条都带：提示词原文 + 中文翻译 + 配图 + 作者 + 来源链接 + 点赞。

特点：
- 直接调官方分页 API（POST /youmarketing-api/prompts），又快又稳，不用浏览器。
- 数据已是精品库，全是真提示词，所以【跳过 Claude 判断】，直接标记为提示词+精品入库，省钱。
- YouMind 是国外站，必须开 VPN（走本地代理 7890）。

用法：  py crawler_youmind.py [页数]
"""
import sys
import json
import time
import datetime
import urllib.request
import socket

import config
from database import get_conn, insert_item, count_items
from analyzer import save_analysis

# 本地代理（VPN）。YouMind 国外站必须走代理。
PROXY = "http://127.0.0.1:7890"

API_URL = "https://youmind.com/youmarketing-api/prompts"

# 要抓的提示词库（model + campaign）。可扩展更多库。
COLLECTIONS = [
    {"model": "nano-banana-pro", "campaign": "nano-banana-pro-prompts",
     "filterMode": "imageCategories", "label": "Nano Banana Pro 图片"},
    {"model": "gpt-image-2", "campaign": "gpt-image-2-prompts",
     "filterMode": "imageCategories", "label": "GPT-Image-2 图片"},
]

LIMIT = 18  # 每页条数（API 默认）


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _make_opener():
    proxy = urllib.request.ProxyHandler({"http": PROXY, "https": PROXY})
    return urllib.request.build_opener(proxy)


def _fetch_page(opener, collection, page, retries=4):
    """抓一页，返回解析后的 dict。失败重试。"""
    body = {
        "model": collection["model"],
        "page": page,
        "limit": LIMIT,
        "locale": "zh-CN",
        "campaign": collection["campaign"],
        "filterMode": collection["filterMode"],
    }
    referer = f"https://youmind.com/zh-CN/{collection['campaign']}"
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(
                API_URL, data=json.dumps(body).encode(), method="POST"
            )
            req.add_header("content-type", "application/json")
            req.add_header("referer", referer)
            req.add_header("accept", "application/json, text/plain, */*")
            req.add_header("accept-language", "zh-CN,zh;q=0.9,en;q=0.8")
            req.add_header(
                "user-agent",
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
            )
            resp = opener.open(req, timeout=30)
            return json.loads(resp.read().decode())
        except Exception as e:
            last_err = e
            if attempt < retries:
                time.sleep(attempt * 1.5)
    raise last_err or RuntimeError(f"抓取第{page}页失败")


def _item_to_record(item, collection):
    """把一条 YouMind prompt 转成我们的统一数据结构。"""
    author = item.get("author") or {}
    media = [m for m in (item.get("media") or []) if isinstance(m, str) and m.strip()]
    content = item.get("content") or ""
    translated = item.get("translatedContent") or ""
    title = item.get("title") or ""
    desc = item.get("description") or ""

    # 正文 = 标题 + 说明 + 提示词原文（便于全文搜索）
    parts = []
    if title:
        parts.append(title)
    if desc:
        parts.append(desc)
    if content:
        parts.append(content)
    full_content = "\n\n".join(parts)

    record = {
        "platform": "YouMind",
        "platform_id": str(item.get("id", "")),
        "content_type": "图片提示词",
        "author_name": author.get("name", ""),
        "author_id": author.get("link", ""),
        "publish_time": item.get("sourcePublishedAt", "") or _now_iso(),
        "title": title,
        "content": full_content,
        "media_urls": " ".join(media),
        "like_count": item.get("likes") or 0,
        "comment_count": 0,
        "share_count": 0,
        "matched_keyword": collection["label"],
        "source_url": item.get("sourceLink", ""),
        "raw_json": json.dumps(item, ensure_ascii=False),
        "crawled_at": _now_iso(),
    }
    # YouMind 数据已是精品，直接附带分析结果（提示词原文/翻译/分类）
    analysis = {
        "is_prompt": 1,
        "quality_score": 85 if item.get("featured") else 78,
        "is_recommended": 1,
        "category": "图片",
        "prompt_text": content,          # 提示词原文（不翻译，宝贵资产）
        "prompt_lang": item.get("language", ""),
        "summary_zh": desc or title,
        "translation_zh": translated,    # YouMind 自带的中文翻译
        "reason": "YouMind 精品提示词库（平台已筛选）",
        "analyzed_at": _now_iso(),
    }
    return record, analysis


def crawl(pages=3, start_page=1):
    """抓 YouMind 各库的前若干页，直接入库（含分析结果）。"""
    opener = _make_opener()
    conn = get_conn()
    total_new = 0

    for col in COLLECTIONS:
        print(f"\n[YouMind] 采集库: {col['label']}")
        for page in range(start_page, start_page + pages):
            try:
                data = _fetch_page(opener, col, page)
            except Exception as e:
                print(f"  第{page}页失败: {e}")
                continue
            prompts = data.get("prompts") or []
            new_this_page = 0
            for item in prompts:
                record, analysis = _item_to_record(item, col)
                if not record["platform_id"]:
                    continue
                if insert_item(conn, record):
                    # 取回 item_id 存分析结果
                    row = conn.execute(
                        "SELECT id FROM items WHERE platform=? AND platform_id=?",
                        ("YouMind", record["platform_id"]),
                    ).fetchone()
                    if row:
                        save_analysis(conn, row[0], analysis)
                    new_this_page += 1
            total_new += new_this_page
            print(f"  第{page}页: 共{len(prompts)}条, 新入库{new_this_page}条 "
                  f"(库总量{data.get('total')})")
            time.sleep(1.5)  # 温和一点

    print(f"\n[YouMind] 本轮共新入库 {total_new} 条，"
          f"数据库 YouMind 累计 {count_items(conn, 'YouMind')} 条")
    conn.close()


if __name__ == "__main__":
    pg = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    crawl(pages=pg)
