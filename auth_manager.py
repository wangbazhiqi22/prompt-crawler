# -*- coding: utf-8 -*-
"""Authentication, ownership, and admin APIs for the prompt crawler app."""

import datetime
import os
import sqlite3
from functools import wraps

from flask import jsonify, redirect, render_template, request, session
from werkzeug.security import check_password_hash, generate_password_hash

from config import DB_PATH
from database import get_conn


DEFAULT_ADMIN_USERNAME = "admin"
from local_settings import initial_admin_password, session_secret


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _dict(row):
    return dict(row) if row else None


def init_auth_db():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            display_name TEXT,
            role TEXT DEFAULT 'user',
            user_category TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1,
            created_at TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS item_user_links (
            user_id INTEGER NOT NULL,
            item_id INTEGER NOT NULL,
            created_at TEXT,
            action_mode TEXT DEFAULT 'parse',
            PRIMARY KEY(user_id, item_id),
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(item_id) REFERENCES items(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS user_actions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            item_id INTEGER,
            action_mode TEXT DEFAULT 'parse',
            source_url TEXT DEFAULT '',
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id),
            FOREIGN KEY(item_id) REFERENCES items(id)
        )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item_links_user ON item_user_links(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_item_links_item ON item_user_links(item_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_user ON user_actions(user_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_user_actions_created ON user_actions(created_at)")

    admin = conn.execute(
        "SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USERNAME,)
    ).fetchone()
    if not admin:
        conn.execute(
            """INSERT INTO users
               (username, password_hash, display_name, role, user_category, is_active, created_at)
               VALUES (?,?,?,?,?,?,?)""",
            (
                DEFAULT_ADMIN_USERNAME,
                generate_password_hash(initial_admin_password()),
                "超级管理员",
                "super_admin",
                "管理",
                1,
                _now_iso(),
            ),
        )
        conn.commit()
        admin = conn.execute(
            "SELECT id FROM users WHERE username=?", (DEFAULT_ADMIN_USERNAME,)
        ).fetchone()

    admin_id = admin["id"]
    conn.execute(
        """INSERT OR IGNORE INTO item_user_links(user_id, item_id, created_at, action_mode)
           SELECT ?, id, COALESCE(crawled_at, ?), 'legacy' FROM items""",
        (admin_id, _now_iso()),
    )
    action_count = conn.execute("SELECT COUNT(*) FROM user_actions").fetchone()[0]
    if action_count == 0:
        conn.execute(
            """INSERT INTO user_actions(user_id, item_id, action_mode, source_url, created_at)
               SELECT ?, id, 'legacy', COALESCE(source_url, ''), COALESCE(crawled_at, ?)
               FROM items""",
            (admin_id, _now_iso()),
        )
    conn.commit()
    conn.close()


def get_user(user_id=None):
    user_id = user_id or session.get("user_id")
    if not user_id:
        return None
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """SELECT id, username, display_name, role, user_category, is_active, created_at
           FROM users WHERE id=?""",
        (user_id,),
    ).fetchone()
    conn.close()
    user = _dict(row)
    if user and user.get("is_active"):
        return user
    return None


def current_user():
    return get_user()


def current_user_id():
    user = current_user()
    return user["id"] if user else None


def is_super_admin(user=None):
    user = user or current_user()
    return bool(user and user.get("role") == "super_admin")


def _wants_json():
    return request.path.startswith("/api/") or "application/json" in (request.headers.get("Accept") or "")


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"ok": False, "error": "未登录"}), 401
            return redirect("/login")
        return fn(*args, **kwargs)

    return wrapper


def admin_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            if _wants_json():
                return jsonify({"ok": False, "error": "未登录"}), 401
            return redirect("/login")
        if not is_super_admin():
            return jsonify({"ok": False, "error": "需要超级管理员权限"}), 403
        return fn(*args, **kwargs)

    return wrapper


BUBBLE_PUBLIC_ASSETS = {
    '/static/bubble/' + name for name in (
        'bubble.css', 'bubble.js',
        'icons/link-simple.svg', 'icons/film-strip.svg', 'icons/magic-wand.svg',
        'icons/arrow-up-right.svg', 'icons/arrow-clockwise.svg',
        'icons/magnifying-glass.svg', 'icons/arrow-right.svg',
    )
} | {
    '/static/ambient/' + name for name in
    ('index.html', 'scene.js', 'login-poster.png', 'workspace-poster.png',
     'login-mobile.png', 'workspace-mobile.png')
}


def _public_path(path):
    return (
        path == "/login"
        or path.startswith("/static/space/")
        or path in BUBBLE_PUBLIC_ASSETS
        or path == "/api/login"
        or path.startswith("/favicon")
    )


def link_item_to_user(item_id, user_id=None, action_mode="parse"):
    if not item_id:
        return
    user_id = user_id or current_user_id()
    if not user_id:
        return
    conn = get_conn()
    conn.execute(
        """INSERT OR IGNORE INTO item_user_links(user_id, item_id, created_at, action_mode)
           VALUES (?,?,?,?)""",
        (int(user_id), int(item_id), _now_iso(), action_mode or "parse"),
    )
    row = conn.execute("SELECT source_url FROM items WHERE id=?", (int(item_id),)).fetchone()
    source_url = row[0] if row else ""
    conn.execute(
        """INSERT INTO user_actions(user_id, item_id, action_mode, source_url, created_at)
           VALUES (?,?,?,?,?)""",
        (int(user_id), int(item_id), action_mode or "parse", source_url or "", _now_iso()),
    )
    conn.commit()
    conn.close()


def user_can_access_item(item_id, user=None):
    user = user or current_user()
    if not user or not item_id:
        return False
    if is_super_admin(user):
        return True
    conn = get_conn()
    row = conn.execute(
        "SELECT 1 FROM item_user_links WHERE user_id=? AND item_id=?",
        (user["id"], item_id),
    ).fetchone()
    conn.close()
    return bool(row)


def unlink_or_delete_item(item_id, user=None):
    user = user or current_user()
    if not user or not item_id:
        return False, "未登录"
    conn = get_conn()
    if not is_super_admin(user):
        cur = conn.execute(
            "DELETE FROM item_user_links WHERE user_id=? AND item_id=?",
            (user["id"], item_id),
        )
        conn.commit()
        conn.close()
        return cur.rowcount > 0, "记录不存在"
    conn.close()
    return True, ""


def _admin_user_rows(q="", category=""):
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    where = []
    args = []
    if q:
        like = f"%{q}%"
        where.append("(u.username LIKE ? OR u.display_name LIKE ?)")
        args.extend([like, like])
    if category:
        where.append("u.user_category=?")
        args.append(category)
    sql_where = "WHERE " + " AND ".join(where) if where else ""
    rows = conn.execute(
        f"""SELECT u.id, u.username, u.display_name, u.role, u.user_category,
                   u.is_active, u.created_at,
                   COUNT(a.id) AS total_records,
                   SUM(CASE WHEN DATE(COALESCE(a.created_at, '')) = DATE('now', 'localtime') THEN 1 ELSE 0 END) AS today_records
            FROM users u
            LEFT JOIN user_actions a ON a.user_id=u.id
            {sql_where}
            GROUP BY u.id
            ORDER BY u.id ASC""",
        args,
    ).fetchall()
    cats = conn.execute(
        "SELECT DISTINCT user_category FROM users WHERE user_category<>'' ORDER BY user_category"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows], [r[0] for r in cats]


def _records_query(user_id=None, q="", platform="", content_type="", category="", limit=300):
    user = current_user()
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    args = []
    where = [
        "(i.matched_keyword='__link__' OR i.platform='抖音')",
        """NOT (
            i.platform='抖音'
            AND i.status='失败'
            AND (i.platform_id LIKE 'dy_pending_%' OR i.title LIKE '解析中%')
            AND EXISTS (
                SELECT 1 FROM items completed
                WHERE completed.id<>i.id
                  AND completed.platform='抖音'
                  AND completed.status='完成'
                  AND completed.source_url=i.source_url
            )
        )""",
    ]
    join = "LEFT JOIN item_user_links l ON l.item_id=i.id LEFT JOIN users u ON u.id=l.user_id"
    if not is_super_admin(user):
        where.append("l.user_id=?")
        args.append(user["id"])
    elif user_id:
        where.append("l.user_id=?")
        args.append(user_id)
    if q:
        like = f"%{q}%"
        where.append("(i.title LIKE ? OR i.content LIKE ? OR i.author_name LIKE ? OR i.source_url LIKE ?)")
        args.extend([like, like, like, like])
    if platform:
        where.append("i.platform=?")
        args.append(platform)
    if content_type:
        where.append("i.content_type=?")
        args.append(content_type)
    if category:
        where.append("a.category=?")
        args.append(category)
    args.append(limit)
    rows = conn.execute(
        f"""SELECT i.id, i.platform, i.content_type, i.status, i.title, i.author_name,
                   i.publish_time, i.content, i.source_url, i.crawled_at, i.raw_json,
                   a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                   a.quality_score, a.is_recommended, a.summary_zh, a.reason,
                   u.id AS owner_user_id, u.username AS owner_username,
                   u.display_name AS owner_display_name, u.user_category AS owner_category
            FROM items i
            LEFT JOIN analysis a ON a.item_id=i.id
            {join}
            WHERE {' AND '.join(where)}
            ORDER BY COALESCE(l.created_at, i.crawled_at) DESC, i.id DESC
            LIMIT ?""",
        args,
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def admin_stats():
    conn = get_conn()
    conn.row_factory = sqlite3.Row
    total_today = conn.execute(
        """SELECT COUNT(*) AS c FROM user_actions
           WHERE DATE(COALESCE(created_at, '')) = DATE('now', 'localtime')"""
    ).fetchone()["c"]
    total_all = conn.execute("SELECT COUNT(*) AS c FROM user_actions").fetchone()["c"]
    by_user = conn.execute(
        """SELECT u.id, u.username, u.display_name, u.user_category,
                  COUNT(a.id) AS total_records,
                  SUM(CASE WHEN DATE(COALESCE(a.created_at, '')) = DATE('now', 'localtime') THEN 1 ELSE 0 END) AS today_records
           FROM users u
           LEFT JOIN user_actions a ON a.user_id=u.id
           GROUP BY u.id
           ORDER BY today_records DESC, total_records DESC"""
    ).fetchall()
    by_platform = conn.execute(
        """SELECT i.platform, COUNT(*) AS c
           FROM user_actions a JOIN items i ON i.id=a.item_id
           WHERE DATE(COALESCE(a.created_at, '')) = DATE('now', 'localtime')
           GROUP BY i.platform ORDER BY c DESC"""
    ).fetchall()
    conn.close()
    return {
        "total_today": total_today,
        "total_all": total_all,
        "by_user": [dict(r) for r in by_user],
        "by_platform": [dict(r) for r in by_platform],
    }


def serialize_record(row):
    d = dict(row)
    try:
        raw = __import__("json").loads(d.pop("raw_json") or "{}")
    except Exception:
        raw = {}
    image_urls = raw.get("image_urls") or []
    if not image_urls:
        image_urls = []
        for p in raw.get("image_paths") or raw.get("frame_paths") or []:
            try:
                import os

                fn = os.path.basename(p)
                key = os.path.basename(os.path.dirname(p))
                if fn and key:
                    image_urls.append(f"/douyin_images/{key}/{fn}")
            except Exception:
                pass
    d["image_urls"] = image_urls
    # Interrupted placeholders used to keep the title "解析中…" even after
    # their status was changed to failed, which made the UI look permanently
    # stuck.  Preserve the stored row, but present an honest display title.
    if d.get("status") == "失败" and str(d.get("title") or "").startswith("解析中"):
        d["title"] = "解析失败"
    d["asset_kind"] = raw.get("kind", "")
    d["has_video"] = bool(raw.get("video_path"))
    d["has_audio"] = bool(raw.get("audio_path"))
    d["has_frames"] = bool(raw.get("frame_paths"))
    d["has_images"] = bool(raw.get("image_paths"))
    manifest = []
    for entry in raw.get("frame_manifest") or []:
        if not isinstance(entry, dict):
            continue
        manifest.append({
            "index": entry.get("index"),
            "time_sec": entry.get("time_sec"),
            "timecode": entry.get("timecode"),
            "shot_id": entry.get("shot_id"),
            "shot_range": entry.get("shot_range"),
            "url": entry.get("url") or "",
        })
    d["frame_manifest"] = manifest
    d["frame_count"] = len(manifest) or len(raw.get("frame_paths") or [])
    d["has_timeline"] = bool(manifest)
    d["has_subtitles"] = bool(raw.get("subtitle_srt") or raw.get("subtitle_segments"))
    return d


def register_auth(app):
    init_auth_db()
    app.secret_key = session_secret()

    @app.after_request
    def bubble_asset_headers(response):
        # Only these public artwork files may be read by the opaque sandbox.
        if request.path in BUBBLE_PUBLIC_ASSETS:
            response.headers['Access-Control-Allow-Origin'] = '*'
            response.headers['Cache-Control'] = 'public, max-age=0, must-revalidate'
        return response

    @app.context_processor
    def inject_user():
        return {"current_user": current_user(), "is_super_admin": is_super_admin()}

    @app.before_request
    def require_auth_for_app():
        if _public_path(request.path):
            return None
        if not current_user():
            if _wants_json():
                return jsonify({"ok": False, "error": "未登录"}), 401
            return redirect("/login")
        protected_item_id = None
        if request.path == "/api/extract/report":
            protected_item_id = request.args.get("id")
        elif request.path in ("/api/bilibili/download", "/api/douyin/download"):
            protected_item_id = request.args.get("item_id")
        if protected_item_id and not user_can_access_item(protected_item_id):
            if _wants_json():
                return jsonify({"ok": False, "error": "无权访问该记录"}), 403
            return "无权访问该记录", 403
        return None

    @app.route("/login")
    def login_page():
        if current_user():
            return redirect("/")
        return render_template("login.html")

    @app.route("/api/login", methods=["POST"])
    def api_login():
        body = request.json or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        conn = get_conn()
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        conn.close()
        if not row or not row["is_active"] or not check_password_hash(row["password_hash"], password):
            return jsonify({"ok": False, "error": "账号或密码错误"}), 401
        session["user_id"] = row["id"]
        return jsonify({"ok": True, "user": get_user(row["id"])})

    @app.route("/logout")
    def logout():
        session.clear()
        return redirect("/login")

    @app.route("/api/me")
    @login_required
    def api_me():
        return jsonify({"ok": True, "user": current_user()})

    @app.route("/admin")
    @admin_required
    def admin_page():
        return render_template("admin.html")

    @app.route("/api/admin/dashboard")
    @admin_required
    def api_admin_dashboard():
        return jsonify({"ok": True, "stats": admin_stats()})

    @app.route("/api/admin/users", methods=["GET", "POST"])
    @admin_required
    def api_admin_users():
        if request.method == "GET":
            users, cats = _admin_user_rows(
                q=(request.args.get("q") or "").strip(),
                category=(request.args.get("category") or "").strip(),
            )
            return jsonify({"ok": True, "users": users, "categories": cats})

        body = request.json or {}
        username = (body.get("username") or "").strip()
        password = body.get("password") or ""
        display_name = (body.get("display_name") or username).strip()
        user_category = (body.get("user_category") or "").strip()
        role = "super_admin" if body.get("role") == "super_admin" else "user"
        if not username or not password:
            return jsonify({"ok": False, "error": "账号和密码不能为空"}), 400
        conn = get_conn()
        try:
            cur = conn.execute(
                """INSERT INTO users(username, password_hash, display_name, role, user_category, is_active, created_at)
                   VALUES (?,?,?,?,?,?,?)""",
                (
                    username,
                    generate_password_hash(password),
                    display_name,
                    role,
                    user_category,
                    1,
                    _now_iso(),
                ),
            )
            conn.commit()
            user_id = cur.lastrowid
        except sqlite3.IntegrityError:
            conn.close()
            return jsonify({"ok": False, "error": "账号已存在"}), 409
        conn.close()
        return jsonify({"ok": True, "id": user_id})

    @app.route("/api/admin/users/<int:user_id>", methods=["PUT"])
    @admin_required
    def api_admin_update_user(user_id):
        body = request.json or {}
        fields = {}
        for key in ("display_name", "user_category"):
            if key in body:
                fields[key] = (body.get(key) or "").strip()
        if "role" in body:
            fields["role"] = "super_admin" if body.get("role") == "super_admin" else "user"
        if "is_active" in body:
            fields["is_active"] = 1 if body.get("is_active") else 0
        conn = get_conn()
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            conn.execute(f"UPDATE users SET {sets} WHERE id=?", list(fields.values()) + [user_id])
        if body.get("password"):
            conn.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (generate_password_hash(body["password"]), user_id),
            )
        conn.commit()
        conn.close()
        return jsonify({"ok": True})

    @app.route("/api/admin/records")
    @admin_required
    def api_admin_records():
        rows = _records_query(
            user_id=request.args.get("user_id") or None,
            q=(request.args.get("q") or "").strip(),
            platform=(request.args.get("platform") or "").strip(),
            content_type=(request.args.get("content_type") or "").strip(),
            category=(request.args.get("category") or "").strip(),
            limit=int(request.args.get("limit") or 300),
        )
        return jsonify({"ok": True, "items": [serialize_record(r) for r in rows], "total": len(rows)})
