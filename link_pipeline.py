# -*- coding: utf-8 -*-
"""
链接分析编排：把「提取 → 入库 → AI分析」串起来。

流程 analyze_and_store(url)：
  1. link_extractor.extract_link(url) 拿结构化内容
  2. 组装 content，insert_item 入 items 表（platform_id=sha1(url) 去重）
  3. analyzer.analyze_one 判定/提取提示词/分类/打分/摘要
  4. 若正文无提示词但有图片 → extract_prompt_from_images 识图补挖，命中则再分析一次
  5. save_analysis 写 analysis 表
  6. 返回 item_id

来源标记：matched_keyword = '__link__'，用于和关键词爬虫数据区分。
"""
import os
import json
import hashlib
import datetime

from database import get_conn, insert_item
import link_extractor

LINK_SOURCE_TAG = "__link__"


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha1(s: str) -> str:
    return hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:16]


def _build_content(data: dict) -> str:
    parts = []
    if data.get("title"):
        parts.append(f"【标题】{data['title']}")
    if data.get("author"):
        parts.append(f"【作者】{data['author']}")
    if data.get("content"):
        parts.append(f"【正文】\n{data['content']}")
    if data.get("video_urls"):
        parts.append("【视频链接】\n" + "\n".join(data.get("video_urls") or []))
    return "\n\n".join(parts)


def _download_images(image_urls, key, platform):
    """把外链图片下到本地 douyin_images/<key>/，复用现有 serve 通道。返回本地路径列表。"""
    if not image_urls:
        return []
    import requests
    from link_extractor import _proxy_reachable, PROXY

    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "douyin_images", key)
    os.makedirs(out_dir, exist_ok=True)

    headers = {"User-Agent": "Mozilla/5.0", "Referer": ""}
    proxies = None
    if platform in ("X", "YouMind") and _proxy_reachable():
        proxies = {"http": PROXY, "https": PROXY}

    local = []
    for i, u in enumerate(image_urls[:15], 1):
        try:
            r = requests.get(u, headers=headers, timeout=30, proxies=proxies)
            if r.status_code == 200 and len(r.content) > 1024:
                p = os.path.join(out_dir, f"{i:02d}.jpg")
                with open(p, "wb") as f:
                    f.write(r.content)
                local.append(p)
        except Exception:
            continue
    return local


def _local_to_url(paths):
    urls = []
    for p in paths:
        fn = os.path.basename(p)
        key = os.path.basename(os.path.dirname(p))
        if fn and key:
            urls.append(f"/douyin_images/{key}/{fn}")
    return urls


def analyze_and_store(url: str, ocr_images: bool = True) -> dict:
    """提取+入库+分析。返回 {ok, item_id, ...} 供 API 直接回给前端。"""
    data = link_extractor.extract_link(url)  # 抓不到会抛异常，由上层兜

    platform = data["platform"]
    key = _sha1(url)
    if platform == "X":
        try:
            _, tweet_id = link_extractor._extract_x_status(url)
            if tweet_id:
                key = f"x_{tweet_id}"
        except Exception:
            pass
    content = _build_content(data)

    # 下载图片到本地（防盗链外链前端可能加载不出，本地 serve 最稳）
    local_imgs = _download_images(data.get("image_urls"), key, platform)
    img_urls = _local_to_url(local_imgs) or data.get("image_urls", [])

    conn = get_conn()
    item = {
        "platform": platform,
        "platform_id": f"link_{key}",
        "content_type": "链接提取",
        "author_name": data.get("author", ""),
        "author_id": "",
        "publish_time": data.get("publish_time", ""),
        "title": data.get("title", ""),
        "content": content,
        "media_urls": " ".join((data.get("image_urls") or []) + (data.get("video_urls") or [])),
        "like_count": 0,
        "comment_count": 0,
        "share_count": 0,
        "matched_keyword": LINK_SOURCE_TAG,
        "source_url": url,
        "raw_json": json.dumps({
            "image_paths": local_imgs,
            "image_urls": img_urls,
            "video_urls": data.get("video_urls") or [],
            "extracted": {k: data.get(k) for k in ("title", "author", "publish_time")},
            "external_meta": data.get("raw_external") or {},
        }, ensure_ascii=False),
        "crawled_at": _now_iso(),
        "status": "完成",
    }
    is_new = insert_item(conn, item)
    row = conn.execute(
        "SELECT id FROM items WHERE platform=? AND platform_id=?",
        (platform, f"link_{key}"),
    ).fetchone()
    item_id = row[0] if row else None

    if not is_new:
        # 已分析过，直接回读
        conn.close()
        return _read_result(item_id, cached=True)

    # AI 分析
    from analyzer import analyze_one, save_analysis
    result = analyze_one(content, platform=platform)

    # 正文没提示词但有图 → 识图补挖
    if ocr_images and not result.get("is_prompt") and local_imgs:
        try:
            from analyzer import extract_prompt_from_images
            ocr = extract_prompt_from_images(local_imgs, desc=data.get("title", ""))
            if ocr and ocr.strip():
                merged = content + "\n\n【图片提示词】\n" + ocr
                conn.execute("UPDATE items SET content=? WHERE id=?", (merged, item_id))
                conn.commit()
                result = analyze_one(merged, platform=platform)
                content = merged
        except Exception:
            pass

    save_analysis(conn, item_id, result)
    conn.close()
    return _read_result(item_id, cached=False)


def _read_result(item_id: int, cached: bool) -> dict:
    """读回一条完整的卡片数据（items + analysis 合并）。"""
    conn = get_conn()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        """SELECT i.id, i.platform, i.title, i.author_name, i.publish_time,
                  i.content, i.source_url, i.crawled_at, i.raw_json,
                  a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                  a.quality_score, a.is_recommended, a.summary_zh, a.reason
           FROM items i LEFT JOIN analysis a ON a.item_id=i.id
           WHERE i.id=?""",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "读取结果失败"}
    d = dict(row)
    try:
        raw = json.loads(d.pop("raw_json") or "{}")
    except Exception:
        raw = {}
    d["image_urls"] = raw.get("image_urls", [])
    d["ok"] = True
    d["cached"] = cached
    return d


if __name__ == "__main__":
    import sys, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    u = sys.argv[1] if len(sys.argv) > 1 else "https://mp.weixin.qq.com/s/RSD9nF20GACmQC6gZQlklg"
    r = analyze_and_store(u)
    r["content"] = (r.get("content") or "")[:300] + "…"
    print(json.dumps(r, ensure_ascii=False, indent=2))
