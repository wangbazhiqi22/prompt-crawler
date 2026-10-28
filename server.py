# -*- coding: utf-8 -*-
"""
网页服务器：把数据库里的内容做成一个可浏览、可搜索、可筛选的网站。
局域网内的同事可以通过 http://你的IP:5000 访问。

页面：
  /            首页（橱窗）—— 所有人浏览各平台最新提示词
  /workspace   工作区 —— 一键提取、分类筛选、批量复制/下载（精选提示词）

启动：  py server.py
"""
import sqlite3
import socket
import io
import os
import csv
import json
import re
import zipfile
from flask import Flask, jsonify, request, render_template, Response, send_file, send_from_directory, redirect

from config import DB_PATH
from text_tools import to_simplified

app = Flask(__name__)

DOUYIN_IMG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "douyin_images")
DOUYIN_IMG_ROOTS = [
    DOUYIN_IMG_ROOT,
    # 兼容项目迁移前已入库的图片绝对路径。旧素材仍然存在，无需复制几十
    # GB 文件；serve 时按作品目录回退读取即可。
    os.path.join(os.path.expanduser("~"), "ai-prompt-crawler", "douyin_images"),
]


@app.route("/douyin_images/<aweme>/<fname>")
def serve_douyin_image(aweme, fname):
    """把本地下载的抖音图文图片 serve 给浏览器看（带防穿越校验）。"""
    if not re.fullmatch(r"[\w\-]+", aweme) or not re.fullmatch(
        r"[\w\-]+\.(jpg|jpeg|png|webp)", fname, re.I
    ):
        return "bad path", 400
    for root in DOUYIN_IMG_ROOTS:
        safe_dir = os.path.join(root, aweme)
        if not os.path.realpath(safe_dir).startswith(os.path.realpath(root)):
            continue
        if os.path.isfile(os.path.join(safe_dir, fname)):
            return send_from_directory(safe_dir, fname)
    return "not found", 404


def _format_image_text(content: str) -> str:
    """把图文帖 content 里的 `--- 图N ---` 标号重排成更易读的导出格式。
    视频帖（无【图片提示词】段）原样返回，不破坏现有 text 下载。"""
    if not content or "【图片提示词】" not in content:
        return content or ""
    sep = "-" * 32
    head, _, tail = content.partition("【图片提示词】")

    # 头部：标题/作者/文案加分隔线
    lines = []
    for raw_line in head.strip().split("\n"):
        ln = raw_line.strip()
        if not ln:
            continue
        if ln.startswith("【标题】"):
            lines.append("标题:  " + ln[len("【标题】"):].strip())
        elif ln.startswith("【作者】"):
            lines.append("作者:  " + ln[len("【作者】"):].strip())
        elif ln.startswith("【文案】"):
            lines.append(sep)
            lines.append("【文案】")
            lines.append(ln[len("【文案】"):].strip())
        else:
            lines.append(ln)
    header = "=" * 32 + "\n" + "\n".join(lines) + "\n" + "=" * 32

    # 图片提示词：--- 图N --- → 【图N】，每张之间加分隔线
    body = re.sub(r"-{2,}\s*图\s*(\d+)\s*-{2,}", lambda m: f"\n{sep}\n【图{m.group(1)}】", tail)
    body = body.strip()
    return header + "\n\n【图片提示词】\n" + body + "\n"


def extract_first_url(text: str) -> str:
    # 与所有平台入口共用同一套富文本/Markdown 安全解析，避免这里再次
    # 吞掉 ``](第二个URL)`` 或中文分享文案尾部标点。
    from share_input import first_url
    return first_url(text)


def query_db(sql, args=()):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, args).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ---------------- 页面 ----------------
@app.route("/")
def home():
    """链接分析首页：贴链接 → 提取正文 + AI判定提取提示词 + 中文摘要。"""
    return render_template("link.html")


@app.route("/browse")
def index():
    """橱窗：各平台原始内容浏览（原首页）。"""
    return render_template("index.html")


@app.route("/api/ai/status")
def local_ai_status():
    from local_ai import health, model_files_ready
    ready, detail = health(timeout=1)
    return jsonify({"backend": "local", "installed": model_files_ready(),
                    "ready": ready, "model": "Qwen3.5-2B Q4", "ocr": "RapidOCR",
                    "detail": detail, "cloud_fallback": False})


@app.route("/workspace")
def workspace():
    return render_template("workspace.html")


@app.route("/douyin")
def douyin_page():
    return render_template("douyin.html")


@app.route("/space")
def space_page():
    return redirect("/login")


# ---------------- 链接分析接口 ----------------
@app.route("/api/extract", methods=["POST"])
def api_extract():
    """
    贴链接分析：抖音走后台异步下载管线，其余平台同步「提取+AI分析」。
    body: {url}
    """
    url = (request.json.get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "请粘贴链接"}), 400

    from link_extractor import detect_platform
    if detect_platform(url) == "抖音":
        # 抖音复用现有后台管线，返回 item_id，前端轮询 /api/douyin/list
        try:
            from crawler_douyin import process_douyin_link
            r = process_douyin_link(url, do_subtitle=True)
            r["async"] = True
            return jsonify(r)
        except Exception as e:
            return jsonify({"ok": False, "error": f"抖音解析失败: {e}"}), 500

    try:
        from link_pipeline import analyze_and_store
        result = analyze_and_store(url)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/extract/list")
def api_extract_list():
    """历史提取记录 feed（仅「贴链接」来源的条目）。"""
    from link_pipeline import LINK_SOURCE_TAG
    rows = query_db(
        """SELECT i.id, i.platform, i.title, i.author_name, i.publish_time,
                  i.content, i.source_url, i.crawled_at, i.raw_json,
                  a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                  a.quality_score, a.is_recommended, a.summary_zh, a.reason
           FROM items i LEFT JOIN analysis a ON a.item_id=i.id
           WHERE i.matched_keyword=?
           ORDER BY i.id DESC LIMIT 100""",
        (LINK_SOURCE_TAG,),
    )
    for r in rows:
        try:
            raw = json.loads(r.pop("raw_json") or "{}")
        except Exception:
            raw = {}
        r["image_urls"] = raw.get("image_urls", [])
    return jsonify({"items": rows, "total": len(rows)})


@app.route("/api/extract/report")
def api_extract_report():
    """把一条提取记录导成 Word(.docx) 报告下载。"""
    item_id = request.args.get("id", "").strip()
    if not item_id:
        return "缺少 id", 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT i.platform, i.title, i.author_name, i.publish_time, i.content,
                  i.source_url, i.crawled_at, i.raw_json,
                  a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                  a.quality_score, a.summary_zh, a.reason
           FROM items i LEFT JOIN analysis a ON a.item_id=i.id WHERE i.id=?""",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return "记录不存在", 404
    d = dict(row)

    from docx import Document
    from docx.shared import Pt, RGBColor

    doc = Document()
    doc.add_heading(d.get("title") or "提取报告", level=0)

    # 元信息表
    meta = [
        ("平台", d.get("platform") or ""),
        ("作者", d.get("author_name") or ""),
        ("发布时间", d.get("publish_time") or ""),
        ("是否含提示词", "是" if d.get("is_prompt") else "否"),
        ("分类", d.get("category") or ""),
        ("质量分", str(d.get("quality_score") or "")),
        ("原链接", d.get("source_url") or ""),
    ]
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for k, v in meta:
        if not v:
            continue
        cells = tbl.add_row().cells
        cells[0].text = k
        cells[1].text = v

    if d.get("summary_zh"):
        doc.add_heading("AI 摘要", level=1)
        doc.add_paragraph(d["summary_zh"])
    if d.get("reason"):
        doc.add_heading("AI 判定理由", level=1)
        doc.add_paragraph(d["reason"])

    if d.get("is_prompt") and (d.get("prompt_text") or "").strip():
        doc.add_heading("提取出的提示词", level=1)
        p = doc.add_paragraph()
        run = p.add_run(d["prompt_text"])
        run.font.name = "Consolas"
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x0B, 0x6B, 0x2E)

    doc.add_heading("完整正文", level=1)
    for para in to_simplified(d.get("content") or "(无正文)").split("\n"):
        doc.add_paragraph(para)

    # 内嵌本地图片
    try:
        raw = json.loads(d.get("raw_json") or "{}")
        paths = [p for p in (raw.get("image_paths") or []) if p and os.path.exists(p)]
        if paths:
            doc.add_heading(f"图片（{len(paths)}）", level=1)
            from docx.shared import Inches
            for p in paths:
                try:
                    doc.add_picture(p, width=Inches(4.5))
                except Exception:
                    pass
    except Exception:
        pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe_title = "".join(c for c in (d.get("title") or "报告") if c not in '\\/:*?"<>|')[:40] or "报告"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{safe_title}.docx",
    )


def _delete_item(item_id) -> bool:
    """删除一条记录：清 items + analysis + 本地下载的图片目录/视频/音频文件。
    返回是否真的删了一条。防穿越：只删 douyin_images 根目录下的文件。"""
    if not item_id:
        return False
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT raw_json FROM items WHERE id=?", (item_id,)).fetchone()
    if not row:
        conn.close()
        return False

    def _safe(p):
        rp = os.path.realpath(p)
        return rp.startswith(os.path.realpath(DOUYIN_IMG_ROOT)) and rp != os.path.realpath(DOUYIN_IMG_ROOT)

    try:
        import shutil
        raw = json.loads(row["raw_json"] or "{}")
        # 图片目录
        for p in (raw.get("image_paths") or []):
            img_dir = os.path.dirname(os.path.abspath(p))
            if _safe(img_dir):
                shutil.rmtree(img_dir, ignore_errors=True)
                break
        # 抖音视频/音频本地文件（在 helper 缓存目录，不强删，仅尝试）
        for key in ("video_path", "audio_path"):
            fp = raw.get(key)
            if fp and os.path.exists(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
    except Exception:
        pass

    conn.execute("DELETE FROM analysis WHERE item_id=?", (item_id,))
    conn.execute("DELETE FROM items WHERE id=?", (item_id,))
    conn.commit()
    conn.close()
    return True


@app.route("/api/extract/delete", methods=["POST"])
def api_extract_delete():
    """删除一条「贴链接」记录。"""
    if _delete_item(request.json.get("id")):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "记录不存在"}), 404


@app.route("/api/item/delete", methods=["POST"])
def api_item_delete():
    """通用删除：按 id 删任意一条记录（橱窗/工作区/抖音共用）。"""
    if _delete_item(request.json.get("id")):
        return jsonify({"ok": True})
    return jsonify({"ok": False, "error": "记录不存在"}), 404


@app.route("/api/douyin/list")
def douyin_list():
    """抖音解析记录列表（详细字段）。"""
    # 看门狗：顺带把超过 10 分钟还卡在「处理中」的僵尸任务标为失败，
    # 避免线程卡死/网络挂起导致前端永久转圈。前端每 5 秒轮询即触发一次。
    try:
        from crawler_douyin import reset_stuck_processing
        reset_stuck_processing(timeout_minutes=10)
    except Exception:
        pass
    rows = query_db(
        """SELECT id, title, author_name, publish_time, content, source_url,
                  status, crawled_at, content_type, raw_json
           FROM items WHERE platform='抖音' ORDER BY id DESC"""
    )
    items = []
    for r in rows:
        content = r.get("content") or ""
        # 估算字幕字数（取【字幕】之后的部分）
        sub_len = 0
        if "【字幕】" in content:
            sub_len = len(content.split("【字幕】", 1)[1].strip())
        raw = {}
        try:
            raw = json.loads(r.get("raw_json") or "{}")
        except Exception:
            pass
        # 本地图片绝对路径 → 拆 aweme目录/文件名 拼成可访问 URL
        img_urls = []
        for p in (raw.get("image_paths") or []):
            fn = os.path.basename(p)
            aweme = os.path.basename(os.path.dirname(p))
            if fn and aweme:
                img_urls.append(f"/douyin_images/{aweme}/{fn}")
        items.append({
            "id": r["id"],
            "title": r.get("title") or "",
            "author_name": r.get("author_name") or "",
            "publish_time": r.get("publish_time") or "",
            "crawled_at": r.get("crawled_at") or "",
            "content": content,
            "source_url": (
                r.get("source_url")
                or raw.get("share_url")
                or extract_first_url(raw.get("share_text") or "")
                or ""
            ),
            "status": r.get("status") or "完成",
            "subtitle_len": sub_len,
            "has_video": bool(raw.get("video_path")),
            "has_audio": bool(raw.get("audio_path")),
            "content_type": r.get("content_type") or "",
            "image_count": raw.get("image_count") or len(img_urls),
            "image_urls": img_urls,
        })
    return jsonify({"items": items, "total": len(items)})


# ---------------- 首页接口 ----------------
@app.route("/api/stats")
def stats():
    total = query_db("SELECT COUNT(*) AS c FROM items")[0]["c"]
    by_platform = query_db(
        "SELECT platform, COUNT(*) AS c FROM items GROUP BY platform ORDER BY c DESC"
    )
    # 已识别为提示词的数量、推荐数量
    prompt_cnt = query_db(
        "SELECT COUNT(*) AS c FROM analysis WHERE is_prompt=1"
    )[0]["c"]
    rec_cnt = query_db(
        "SELECT COUNT(*) AS c FROM analysis WHERE is_recommended=1"
    )[0]["c"]
    cats = [
        r["category"]
        for r in query_db(
            "SELECT category, COUNT(*) c FROM analysis WHERE is_prompt=1 "
            "AND category IS NOT NULL AND category<>'' GROUP BY category ORDER BY c DESC"
        )
    ]
    return jsonify({
        "total": total,
        "by_platform": by_platform,
        "platforms": [r["platform"] for r in by_platform],
        "prompt_count": prompt_cnt,
        "recommended_count": rec_cnt,
        "categories": cats,
    })


@app.route("/api/items")
def items():
    """首页列表：展示所有内容（含未分析的），用于浏览各平台新鲜内容。"""
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    sort = request.args.get("sort", "time").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = 30
    offset = (page - 1) * page_size

    where, params = [], []
    if q:
        where.append("(i.content LIKE ? OR i.author_name LIKE ? OR i.matched_keyword LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if platform:
        where.append("i.platform = ?")
        params.append(platform)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    order_sql = "ORDER BY i.like_count DESC, i.publish_time DESC" if sort == "like" else "ORDER BY i.publish_time DESC"

    total = query_db(f"SELECT COUNT(*) c FROM items i {where_sql}", params)[0]["c"]
    rows = query_db(
        f"""SELECT i.id, i.platform, i.content_type, i.author_name, i.publish_time,
                   i.title, i.content, i.media_urls, i.like_count, i.comment_count,
                   i.share_count, i.matched_keyword, i.source_url, i.status,
                   a.is_prompt, a.category, a.summary_zh, a.translation_zh
            FROM items i LEFT JOIN analysis a ON a.item_id=i.id
            {where_sql} {order_sql} LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    )
    return jsonify({"total": total, "page": page, "page_size": page_size, "items": rows})


# ---------------- 工作区接口 ----------------
@app.route("/api/workspace")
def workspace_items():
    """
    工作区：只看【被判定为真·提示词】的精华内容。
    支持按分类、平台、是否只看推荐、关键词、排序筛选。
    """
    q = request.args.get("q", "").strip()
    platform = request.args.get("platform", "").strip()
    category = request.args.get("category", "").strip()
    only_rec = request.args.get("only_recommended", "").strip()
    sort = request.args.get("sort", "score").strip()
    page = max(1, int(request.args.get("page", 1)))
    page_size = 24
    offset = (page - 1) * page_size

    where = ["a.is_prompt=1"]
    params = []
    if q:
        where.append("(a.prompt_text LIKE ? OR a.summary_zh LIKE ? OR i.author_name LIKE ?)")
        like = f"%{q}%"
        params += [like, like, like]
    if platform:
        where.append("i.platform=?")
        params.append(platform)
    if category:
        where.append("a.category=?")
        params.append(category)
    if only_rec == "1":
        where.append("a.is_recommended=1")
    where_sql = "WHERE " + " AND ".join(where)

    if sort == "time":
        order_sql = "ORDER BY i.publish_time DESC"
    elif sort == "like":
        order_sql = "ORDER BY i.like_count DESC"
    else:
        order_sql = "ORDER BY a.quality_score DESC, i.like_count DESC"

    total = query_db(
        f"SELECT COUNT(*) c FROM analysis a JOIN items i ON i.id=a.item_id {where_sql}",
        params,
    )[0]["c"]
    rows = query_db(
        f"""SELECT i.id, i.platform, i.author_name, i.publish_time, i.source_url,
                   i.like_count, i.media_urls,
                   a.category, a.quality_score, a.is_recommended,
                   a.prompt_text, a.prompt_lang, a.summary_zh, a.translation_zh, a.reason
            FROM analysis a JOIN items i ON i.id=a.item_id
            {where_sql} {order_sql} LIMIT ? OFFSET ?""",
        params + [page_size, offset],
    )
    return jsonify({"total": total, "page": page, "page_size": page_size, "items": rows})


@app.route("/api/export")
def export_prompts():
    """批量下载：把当前筛选条件下的提示词导成 CSV（含提示词原文）。"""
    category = request.args.get("category", "").strip()
    platform = request.args.get("platform", "").strip()
    only_rec = request.args.get("only_recommended", "").strip()

    where = ["a.is_prompt=1"]
    params = []
    if platform:
        where.append("i.platform=?"); params.append(platform)
    if category:
        where.append("a.category=?"); params.append(category)
    if only_rec == "1":
        where.append("a.is_recommended=1")
    where_sql = "WHERE " + " AND ".join(where)

    rows = query_db(
        f"""SELECT a.category, i.platform, i.author_name, i.publish_time,
                   a.quality_score, a.prompt_text, a.summary_zh, i.source_url
            FROM analysis a JOIN items i ON i.id=a.item_id
            {where_sql} ORDER BY a.quality_score DESC""",
        params,
    )
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["分类", "来源平台", "作者", "发布时间", "质量分", "提示词原文", "中文说明", "原文链接"])
    for r in rows:
        w.writerow([r["category"], r["platform"], r["author_name"], r["publish_time"],
                    r["quality_score"], r["prompt_text"], r["summary_zh"], r["source_url"]])
    csv_data = "﻿" + buf.getvalue()  # BOM 让 Excel 正确识别中文
    return Response(
        csv_data,
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=prompts.csv"},
    )


@app.route("/api/translate", methods=["POST"])
def translate():
    """
    按需翻译某条内容的说明文字（提示词本体不翻）。带缓存：
    翻过的存进 analysis.translation_zh，下次直接读，不重复花钱。
    """
    item_id = request.json.get("item_id")
    if not item_id:
        return jsonify({"error": "缺少 item_id"}), 400

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # 先查缓存
    row = conn.execute(
        "SELECT translation_zh FROM analysis WHERE item_id=?", (item_id,)
    ).fetchone()
    if row and row["translation_zh"]:
        conn.close()
        return jsonify({"translation": row["translation_zh"], "cached": True})

    # 拿原文
    item = conn.execute("SELECT content FROM items WHERE id=?", (item_id,)).fetchone()
    if not item:
        conn.close()
        return jsonify({"error": "找不到该内容"}), 404

    try:
        from analyzer import translate_text
        zh = translate_text(item["content"])
    except Exception as e:
        conn.close()
        return jsonify({"error": f"翻译失败: {e}"}), 500

    # 写缓存（确保该 item 在 analysis 表里有行）
    conn.execute(
        "INSERT INTO analysis (item_id, translation_zh) VALUES (?, ?) "
        "ON CONFLICT(item_id) DO UPDATE SET translation_zh=excluded.translation_zh",
        (item_id, zh),
    )
    conn.commit()
    conn.close()
    return jsonify({"translation": zh, "cached": False})


@app.route("/api/add_douyin", methods=["POST"])
def add_douyin():
    """
    粘贴抖音链接 → 后台下载+本地Whisper转字幕。
    立即返回 item_id（status=处理中），前端轮询/刷新看结果。
    """
    share = (request.json.get("share_text") or "").strip()
    do_sub = request.json.get("subtitle", True)
    mode = (request.json.get("mode") or "parse").strip()
    mode = "lapian" if mode == "lapian" else "parse"
    if not share:
        return jsonify({"ok": False, "error": "请粘贴抖音分享链接"}), 400
    try:
        from crawler_douyin import process_douyin_link
        result = process_douyin_link(share, do_subtitle=do_sub, mode=mode)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


def _douyin_report(item_id):
    """生成抖音视频的 Word 报告：「何止维-提示词视频字幕要点总结」格式。"""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT i.id, i.title, i.author_name, i.publish_time, i.content,
                  i.source_url, i.crawled_at, i.raw_json, i.platform,
                  a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                  a.quality_score, a.summary_zh, a.reason
           FROM items i LEFT JOIN analysis a ON a.item_id=i.id
           WHERE i.id=?""",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return "找不到该抖音记录", 404
    d = dict(row)
    title = d.get("title") or "douyin"
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')[:40] or "douyin"

    from docx import Document
    from docx.shared import Pt, RGBColor, Inches

    doc = Document()
    # 封面标题
    doc.add_heading(title, level=0)
    doc.add_paragraph("提示词视频字幕要点总结", style="Subtitle")

    # 元信息表
    meta = [
        ("作者", d.get("author_name") or ""),
        ("平台", d.get("platform") or "抖音"),
        ("解析时间", d.get("crawled_at") or ""),
        ("原链接", d.get("source_url") or ""),
        ("是否含提示词", "是" if d.get("is_prompt") else "否"),
        ("分类", d.get("category") or "未分析"),
        ("质量评分", str(d.get("quality_score") or "")),
    ]
    doc.add_heading("基本信息", level=1)
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for k, v in meta:
        if not v:
            continue
        cells = tbl.add_row().cells
        cells[0].text = k
        cells[1].text = v

    # AI 要点总结
    doc.add_heading("一、AI 要点总结", level=1)
    summary = (d.get("summary_zh") or "").strip()
    if summary:
        doc.add_paragraph(summary)
    elif d.get("reason") == "no_key":
        doc.add_paragraph("（本次尚未完成本地 AI 分析，可保留素材后重试。）")
    else:
        doc.add_paragraph('（暂无本地 AI 分析结果，请先解析素材和生成字幕。）')

    # 提取的提示词
    doc.add_heading("二、提取的提示词", level=1)
    prompt = (d.get("prompt_text") or "").strip()
    if prompt:
        p = doc.add_paragraph()
        run = p.add_run(prompt)
        run.font.name = "Consolas"
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x0B, 0x6B, 0x2E)
    else:
        doc.add_paragraph("（未从字幕中提取到提示词）")

    # 完整字幕
    doc.add_heading("三、完整字幕", level=1)
    content = d.get("content") or ""
    # 拆出【字幕】段
    sub_text = ""
    for part in content.split("\n\n"):
        if part.startswith("【字幕】"):
            sub_text = part.replace("【字幕】", "").strip()
            break
    if sub_text:
        for line in sub_text.split("\n"):
            doc.add_paragraph(line)
    else:
        doc.add_paragraph("（无字幕）")

    graphic_text = ""
    graphic_heading = ""
    for part in content.split("\n\n"):
        if part.startswith("【视频图文信息提取】"):
            graphic_heading = "四、视频图文信息提取"
            graphic_text = part.replace("【视频图文信息提取】", "").strip()
            break
        if part.startswith("【图片图文信息提取】"):
            graphic_heading = "四、图片图文信息提取"
            graphic_text = part.replace("【图片图文信息提取】", "").strip()
            break
        if part.startswith("【图片提示词】"):
            graphic_heading = "四、图片提示词/图文信息提取"
            graphic_text = part.replace("【图片提示词】", "").strip()
            break
    if graphic_text:
        doc.add_heading(graphic_heading, level=1)
        for line in graphic_text.split("\n"):
            doc.add_paragraph(line)

    visual_text = ""
    visual_heading = ""
    for part in content.split("\n\n"):
        if part.startswith("【拉片分析】"):
            visual_heading = "四、拉片分析"
            visual_text = part.replace("【拉片分析】", "").strip()
            break
        if part.startswith("【关键帧视觉主分析】"):
            visual_heading = "四、关键帧视觉主分析"
            visual_text = part.replace("【关键帧视觉主分析】", "").strip()
            break
        if part.startswith("【画面内容分析】"):
            visual_heading = "四、画面内容分析"
            visual_text = part.replace("【画面内容分析】", "").strip()
            break
    if visual_text:
        doc.add_heading(visual_heading or ("五、画面内容分析" if graphic_text else "四、画面内容分析"), level=1)
        for line in visual_text.split("\n"):
            doc.add_paragraph(line)

    # 原文案
    body_text = ""
    for part in content.split("\n\n"):
        if part.startswith("【文案】"):
            body_text = part.replace("【文案】", "").strip()
            break
    if body_text:
        doc.add_heading("六、原文案" if graphic_text and visual_text else "五、原文案", level=1)
        doc.add_paragraph(body_text)

    # 内嵌图片
    try:
        raw = json.loads(d.get("raw_json") or "{}")
        paths = [p for p in (raw.get("image_paths") or []) if p and os.path.exists(p)]
        if paths:
            doc.add_heading(f"六、图片/关键帧（{len(paths)}张）", level=1)
            for p in paths:
                try:
                    doc.add_picture(p, width=Inches(4.5))
                except Exception:
                    pass
    except Exception:
        pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{safe_title}_提示词要点总结.docx",
    )


@app.route("/api/douyin/ocr", methods=["POST"])
def douyin_ocr():
    """手动触发一条图文帖的识图提取提示词（花钱、耗时，故不自动跑）。"""
    item_id = (request.json or {}).get("item_id")
    if not item_id:
        return jsonify({"ok": False, "error": "缺少 item_id"}), 400
    try:
        from crawler_douyin import run_ocr_for_item
        return jsonify(run_ocr_for_item(item_id))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/douyin/download")
def douyin_download():
    """
    抖音素材按需下载：kind=video|audio|text|report|images。
    report 生成 Word(.docx) 报告；其余从 raw_json 取本地文件路径。
    """
    item_id = request.args.get("item_id", "").strip()
    kind = request.args.get("kind", "text").strip()
    if not item_id:
        return "缺少 item_id", 400

    if kind == "report":
        return _douyin_report(item_id)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT title, content, raw_json FROM items WHERE id=? AND platform='抖音'",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return "找不到该抖音记录", 404

    title = row["title"] or "douyin"
    safe_title = "".join(c for c in title if c not in '\\/:*?"<>|')[:40] or "douyin"

    if kind == "text":
        # 文本直接拼成 txt 附件（图文帖过一遍排版优化）
        buf = io.BytesIO(("﻿" + _format_image_text(row["content"] or "")).encode("utf-8"))
        return send_file(
            buf, mimetype="text/plain; charset=utf-8",
            as_attachment=True, download_name=f"{safe_title}.txt",
        )

    if kind == "images":
        # 把该条所有本地图片打包成内存 zip 下载
        try:
            raw = json.loads(row["raw_json"] or "{}")
        except Exception:
            raw = {}
        paths = [p for p in (raw.get("image_paths") or []) if p and os.path.exists(p)]
        if not paths:
            return "没有可下载的图片（可能已被清理，请重新解析）", 404
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, p in enumerate(paths, 1):
                ext = os.path.splitext(p)[1] or ".jpg"
                zf.write(p, arcname=f"{i:02d}{ext}")
        mem.seek(0)
        return send_file(
            mem, mimetype="application/zip",
            as_attachment=True, download_name=f"{safe_title}_images.zip",
        )

    # video / audio：从 raw_json 取本地路径
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except Exception:
        raw = {}
    if kind == "srt":
        srt_text = (raw.get("subtitle_srt") or "").strip()
        if not srt_text:
            return "没有可下载的带时间码字幕（请重新解析并启用字幕）", 404
        buf = io.BytesIO(("﻿" + srt_text).encode("utf-8"))
        return send_file(
            buf,
            mimetype="application/x-subrip; charset=utf-8",
            as_attachment=True,
            download_name=f"{safe_title}.srt",
        )
    path_key = "video_path" if kind == "video" else "audio_path"
    file_path = raw.get(path_key, "")
    if not file_path or not os.path.exists(file_path):
        return f"文件不存在（可能已被清理，请重新解析）", 404
    ext = os.path.splitext(file_path)[1] or (".mp4" if kind == "video" else ".mp3")
    return send_file(file_path, as_attachment=True, download_name=f"{safe_title}{ext}")


def get_lan_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    from run_server_with_extensions import main
    main()
