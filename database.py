# -*- coding: utf-8 -*-
"""
数据库层：所有平台的数据都存进同一张表，字段统一。
后续做筛选/去重/统计都基于这张表。
"""
import os
import sqlite3
import threading
from config import DB_PATH


_SCHEMA_LOCK = threading.Lock()
_SCHEMA_READY = False

# 统一的数据结构。每个平台的爬虫都把结果整理成这些字段。
# 这是你说的「来源、时间、类型、详细内容」的落地。
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    platform        TEXT,      -- 来源平台: X / 小红书 / 抖音 / 快手 / 微信公众号
    platform_id     TEXT,      -- 该内容在平台上的唯一ID（用于去重）
    content_type    TEXT,      -- 类型: 帖子/视频/笔记/文章/图文
    author_name     TEXT,      -- 作者昵称
    author_id       TEXT,      -- 作者ID
    publish_time    TEXT,      -- 发布时间(ISO字符串)
    title           TEXT,      -- 标题（有的平台没有则留空）
    content         TEXT,      -- 详细内容正文/字幕/笔记文字
    media_urls      TEXT,      -- 图片/视频链接(多个用空格隔开)
    like_count      INTEGER,   -- 点赞/喜欢数
    comment_count   INTEGER,   -- 评论数
    share_count     INTEGER,   -- 转发/分享数
    matched_keyword TEXT,      -- 是被哪个关键词搜到的
    source_url      TEXT,      -- 原文链接
    raw_json        TEXT,      -- 原始数据备份(以防漏字段，后续可回溯)
    crawled_at      TEXT,      -- 抓取时间
    status          TEXT DEFAULT '完成',  -- 处理状态: 处理中/完成/失败(主要给抖音后台处理用)
    UNIQUE(platform, platform_id)
);

-- AI 分析结果表：每条 item 经 Claude 判断后的产物。
-- 与 items 分开存，方便重跑分析、也方便没分析过的内容先不显示在精选区。
CREATE TABLE IF NOT EXISTS analysis (
    item_id         INTEGER PRIMARY KEY,  -- 对应 items.id
    is_prompt       INTEGER,   -- 是否是「真·提示词」: 1是 0否
    quality_score   INTEGER,   -- 质量评分 0-100，越高越值得收藏
    is_recommended  INTEGER,   -- 是否推荐(高质量精选): 1是 0否
    category        TEXT,      -- 分类: 图片/光影/角色/分镜/视频/写作/通用 等
    prompt_text     TEXT,      -- 【提取出的提示词正文】—— 原文，绝不翻译，宝贵资产
    prompt_lang     TEXT,      -- 提示词语言: zh/en/其他
    summary_zh      TEXT,      -- 这条内容的中文一句话说明(给看不懂的人)
    translation_zh  TEXT,      -- 周边说明文字的中文翻译(提示词本体不翻)
    reason          TEXT,      -- AI 判断理由(为什么推荐/为什么不是提示词)
    analyzed_at     TEXT,      -- 分析时间
    FOREIGN KEY(item_id) REFERENCES items(id)
);
"""


def get_conn():
    global _SCHEMA_READY
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    # 后台的视频、图文和 AI 分析会并发写库。WAL 允许读取和写入并行，
    # busy_timeout 则让短暂写锁等待而不是直接把任务线程打崩。
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.OperationalError:
        pass
    # 旧实现每开一个连接都 executescript，多个抖音任务同时结束时会争抢
    # schema 锁。每个进程只初始化一次即可。
    if not _SCHEMA_READY:
        with _SCHEMA_LOCK:
            if not _SCHEMA_READY:
                conn.executescript(SCHEMA)
                cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
                if "status" not in cols:
                    conn.execute("ALTER TABLE items ADD COLUMN status TEXT DEFAULT '完成'")
                    conn.commit()
                _SCHEMA_READY = True
    return conn


def insert_item(conn, item: dict) -> bool:
    """插入一条。重复(platform+platform_id相同)则跳过。返回是否新插入。"""
    cols = [
        "platform", "platform_id", "content_type", "author_name", "author_id",
        "publish_time", "title", "content", "media_urls", "like_count",
        "comment_count", "share_count", "matched_keyword", "source_url",
        "raw_json", "crawled_at", "status",
    ]
    placeholders = ",".join(["?"] * len(cols))
    sql = f"INSERT OR IGNORE INTO items ({','.join(cols)}) VALUES ({placeholders})"
    cur = conn.execute(sql, [item.get(c, "完成" if c == "status" else None) for c in cols])
    conn.commit()
    return cur.rowcount > 0


def count_items(conn, platform: str = None) -> int:
    if platform:
        return conn.execute(
            "SELECT COUNT(*) FROM items WHERE platform=?", (platform,)
        ).fetchone()[0]
    return conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
