# -*- coding: utf-8 -*-
"""抖音链接解析、视频下载和本地字幕/画面分析。"""
import os
from browser_support import launch_browser
import sys
import json
import threading
import datetime
import re
import socket
import hashlib
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import config
from database import get_conn
from share_input import extract_douyin_command, first_url, normalize_douyin_share_text
from text_tools import to_simplified

_DOUYIN_BROWSER_LOCK = threading.Lock()
_ACTIVE_JOB_LOCK = threading.Lock()
_ACTIVE_JOBS = {}


def _extract_share_url(text: str) -> str:
    normalized = normalize_douyin_share_text(text)
    return first_url(normalized) or normalized.strip()


def _modal_id_video_url(text: str) -> str | None:
    """把 douyin.com/friend?modal_id=... 这类页面转成视频直达页，减少外部解析/浏览器兜底超时。"""
    url = _extract_share_url(text)
    if not url:
        return None
    try:
        parts = urlsplit(url)
        modal_id = (parse_qs(parts.query).get("modal_id") or [""])[0]
    except Exception:
        modal_id = ""
    if not modal_id:
        m = re.search(r"[?&]modal_id=(\d{15,})", url)
        modal_id = m.group(1) if m else ""
    if modal_id and re.fullmatch(r"\d{15,}", modal_id):
        return f"https://www.douyin.com/video/{modal_id}"
    return None


def _download_share_text(share_text: str) -> str:
    normalized = normalize_douyin_share_text(share_text)
    return _modal_id_video_url(normalized) or normalized


def _is_proxy_reachable(proxy_url: str) -> bool:
    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((parsed.hostname, port), timeout=0.7):
            return True
    except OSError:
        return False


def _configure_douyin_proxy() -> str | None:
    """Use the local proxy only when it is actually running."""
    proxy = os.environ.get("DOUYIN_PROXY", "http://127.0.0.1:7890").strip()
    if not proxy or proxy.lower() == "none":
        os.environ["DOUYIN_PROXY"] = "none"
        return None
    if _is_proxy_reachable(proxy):
        os.environ["DOUYIN_PROXY"] = proxy
        return proxy
    os.environ["DOUYIN_PROXY"] = "none"
    return None


def _make_session():
    """新建一个抖音请求用的 session。
    - 本地代理(如 127.0.0.1:7890)可达：显式挂代理。
    - 本地代理不可达：放行系统代理(trust_env=True)，覆盖 TUN/系统代理模式的 VPN。
    解析接口在 Vercel(海外)，国内直连会被墙，必须走某种代理。
    """
    import requests
    s = requests.Session()
    proxy = _configure_douyin_proxy()
    if proxy:
        s.trust_env = False
        s.proxies = {"http": proxy, "https": proxy}
    else:
        s.trust_env = True
    return s


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _update_item(item_id, **fields):
    """更新一条 item 的指定字段。"""
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE items SET {sets} WHERE id=?", list(fields.values()) + [item_id])
    conn.commit()
    conn.close()


def reset_stuck_processing(timeout_minutes=None):
    """把僵尸「处理中」记录标记为失败。

    抖音解析走 daemon 后台线程，一旦线程猝死（服务器重启 / 网络挂起卡住 /
    进程被杀），数据库记录会永久停在「处理中」导致前端一直转圈。

    - timeout_minutes=None：清理【全部】处理中记录。用于服务器启动那一刻
      （此时所有旧线程都已随进程死亡，无一存活）。
    - timeout_minutes=N：只清理【超过 N 分钟】还没完成的记录（看门狗模式）。
      正常任务几分钟内会完成，超过 N 分钟基本可判定线程已卡死/丢失。
      用于运行期间定时兜底，不误杀刚提交、正在正常跑的任务。

    返回清理的条数。"""
    import datetime
    try:
        conn = get_conn()
        if timeout_minutes is None:
            cur = conn.execute(
                "UPDATE items SET status='失败', "
                "content='解析被中断（服务器重启导致后台任务丢失），请点「重新解析」重试。' "
                "WHERE platform='抖音' AND status='处理中'"
            )
        else:
            cutoff = (datetime.datetime.now()
                      - datetime.timedelta(minutes=timeout_minutes)).isoformat()
            cur = conn.execute(
                "UPDATE items SET status='失败', "
                "content='解析超时（可能网络卡住或抖音风控），请点「重新解析」重试。' "
                "WHERE platform='抖音' AND status='处理中' AND crawled_at < ?",
                (cutoff,),
            )
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n
    except Exception:
        return 0


def create_placeholder(share_text, mode="parse", canonical_id="") -> int:
    """先入库一条占位记录(处理中)，返回 item_id。"""
    share_url = _extract_share_url(share_text)
    conn = get_conn()
    # 短链重定向已经拿到作品 ID 时，直接以作品 ID 去重；否则再用 URL 的
    # 稳定摘要占位。这样重复点击“解析”会复用原记录，不会在任务结束时因
    # platform_id 唯一键冲突而把后台线程打崩。
    dedupe_source = share_url or normalize_douyin_share_text(share_text)
    canonical_id = str(canonical_id or "").strip()
    pid = canonical_id or ("dy_pending_" + hashlib.sha1(dedupe_source.encode("utf-8")).hexdigest()[:20])
    item = {
        "platform": "抖音",
        "platform_id": pid,
        "content_type": "提示词反推" if mode == "reverse_prompt" else ("拉片" if mode == "lapian" else "视频"),
        "author_name": "",
        "author_id": "",
        "publish_time": _now_iso(),
        "title": "首帧反推中…" if mode == "reverse_prompt" else ("拉片中…" if mode == "lapian" else "解析中…"),
        "content": (
            "正在下载素材并反推 MJ 与 GPT 两套首帧/文生图提示词…"
            if mode == "reverse_prompt" else
            "正在后台下载视频并转字幕，请稍候…"
        ),
        "media_urls": "",
        "like_count": 0,
        "comment_count": 0,
        "share_count": 0,
        "matched_keyword": "手动添加",
        "source_url": share_url,
        "raw_json": json.dumps({"share_text": share_text, "analysis_mode": mode}, ensure_ascii=False),
        "crawled_at": _now_iso(),
        "status": "处理中",
    }
    # 优先复用 canonical id；旧记录尚未拿到 canonical id 时，再按 source_url
    # 复用。更新原记录可保证前端轮询的 item_id 一直有效。
    row = conn.execute(
        "SELECT id FROM items WHERE platform=? AND platform_id=? ORDER BY id DESC LIMIT 1",
        ("抖音", pid),
    ).fetchone()
    if not row and share_url:
        row = conn.execute(
            "SELECT id FROM items WHERE platform=? AND source_url=? ORDER BY id DESC LIMIT 1",
            ("抖音", share_url),
        ).fetchone()
    if row:
        item_id = row[0]
        assignments = ",".join(f"{key}=?" for key in item)
        conn.execute(
            f"UPDATE items SET {assignments} WHERE id=?",
            list(item.values()) + [item_id],
        )
        conn.commit()
        conn.close()
        return item_id

    from database import insert_item
    insert_item(conn, item)
    row = conn.execute(
        "SELECT id FROM items WHERE platform=? AND platform_id=?", ("抖音", pid)
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _do_process(item_id, share_text, do_subtitle, mode="parse"):
    """后台线程实际干活：下载 + 转字幕 + 更新记录。"""
    try:
        # 先调解析接口判断类型（视频 or 图文）
        parsed = _parse_douyin(share_text)
        is_image_post = parsed and not parsed.get("video_url") and parsed.get("image_url_list")

        if is_image_post:
            _process_image_post(item_id, share_text, parsed, mode=mode)
        else:
            _process_video_post(item_id, share_text, do_subtitle, mode=mode, parsed=parsed)
    except Exception as e:
        hint = ("解析失败。现在视频/图文都优先走国内直连，一般不需要代理。\n"
                "若偶发失败（抖音风控波动），点「重新解析」重试通常就好。")
        _update_item(
            item_id,
            title="解析失败",
            content=f"处理出错：{e}\n\n{hint}",
            source_url=_extract_share_url(share_text),
            raw_json=json.dumps({"share_text": share_text, "error": str(e)}, ensure_ascii=False),
            status="失败",
        )


def _run_process_job(job_key, item_id, share_text, do_subtitle, mode):
    try:
        _do_process(item_id, share_text, do_subtitle, mode)
    finally:
        with _ACTIVE_JOB_LOCK:
            _ACTIVE_JOBS.pop(job_key, None)


_MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 15_0 like Mac OS X) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.0 Mobile/15E148 Safari/604.1")


def _direct_session():
    """国内直连专用 session：不走任何代理（抖音自家域名国内直连最快最稳）。"""
    import requests
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    return s


def _resolve_douyin_redirect(share_text):
    """Resolve a Douyin share URL with the mobile route and identify its post type.

    Douyin's old ``/share/video`` page no longer embeds ``play_addr`` reliably,
    but the mobile redirect still exposes the canonical post id and whether the
    target is a video or an image note.  This is enough to keep type detection
    local and avoid waiting on the overseas parser for ordinary video links.
    """
    url = _extract_share_url(share_text)
    if not url:
        return None

    def classify(candidate):
        try:
            path = urlsplit(candidate).path.lower()
        except Exception:
            path = ""
        match = re.search(r"/(?:share/)?(video|note|slides)/(\d{15,})", path)
        if match:
            route, aweme_id = match.groups()
            return {
                "kind": "video" if route == "video" else "image",
                "aweme_id": aweme_id,
                "resolved_url": candidate,
            }
        # Some Douyin desktop redirects no longer expose /video/<id> in the
        # path and instead land on /friend or /user/self with modal_id.  It is
        # still a usable canonical video id (image notes retain /note in the
        # mobile redirect, so this does not steal the normal image path).
        try:
            modal_id = (parse_qs(urlsplit(candidate).query).get("modal_id") or [""])[0]
        except Exception:
            modal_id = ""
        if re.fullmatch(r"\d{15,}", modal_id):
            return {
                "kind": "video",
                "aweme_id": modal_id,
                "resolved_url": f"https://www.douyin.com/video/{modal_id}",
            }
        return None

    direct = classify(url)
    if direct:
        return direct

    try:
        response = _direct_session().get(
            url,
            headers={"User-Agent": _MOBILE_UA, "Referer": "https://www.douyin.com/"},
            timeout=20,
            allow_redirects=True,
        )
        response.raise_for_status()
        resolved = classify(response.url)
        if resolved:
            return resolved
        # A few anti-bot responses finish on a generic page even though an
        # earlier Location header contained the real route.  Preserve that
        # useful redirect evidence before giving up.
        for hop in reversed(response.history):
            location = hop.headers.get("location") or ""
            resolved = classify(location)
            if resolved:
                return resolved
    except Exception:
        return None
    return None


def _parse_douyin_image_local(share_text):
    """本地解析抖音图文帖（国内直连，不依赖海外接口）。
    做法：短链跳转到 /share/note/<id>/ 页面 → 抠 window._ROUTER_DATA JSON → 取图集 url_list。
    失败返回 None。"""
    import re
    url = _extract_share_url(share_text)
    s = _direct_session()
    headers = {"User-Agent": _MOBILE_UA}
    try:
        r = s.get(url, headers=headers, timeout=15, allow_redirects=True)
        final_url = r.url
        m = re.search(r'(?:note|slides|video)/(\d+)', final_url) or re.search(r'(\d{15,})', final_url)
        aweme_id = m.group(1) if m else None

        html = r.text
        # 若跳转后不是 note 页，尝试显式请求 note 分享页
        if "_ROUTER_DATA" not in html and aweme_id:
            note_url = f"https://www.iesdouyin.com/share/note/{aweme_id}/?from_ssr=1"
            html = s.get(note_url, headers=headers, timeout=15).text

        mm = re.search(r'window\._ROUTER_DATA\s*=\s*(\{.*?\})\s*</script>', html, re.S)
        if not mm:
            return None
        data = json.loads(mm.group(1))

        found = {}

        def walk(o):
            if isinstance(o, dict):
                if o.get("images") and isinstance(o["images"], list) and not found:
                    imgs = []
                    for img in o["images"]:
                        if isinstance(img, dict):
                            lst = img.get("url_list") or img.get("download_url_list") or []
                            if lst:
                                imgs.append(lst[0])
                    if imgs:
                        found["image_url_list"] = imgs
                        found["desc"] = o.get("desc") or o.get("title") or ""
                        found["nickname"] = (o.get("authorInfo") or {}).get("nickname") \
                            or (o.get("author") or {}).get("nickname") or ""
                        found["aweme_id"] = str(o.get("aweme_id") or aweme_id or "")
                        found["digg_count"] = ((o.get("statistics") or {}).get("digg_count")) or 0
                        return True
                for v in o.values():
                    if walk(v):
                        return True
            elif isinstance(o, list):
                for v in o:
                    if walk(v):
                        return True
            return False

        walk(data)
        if found.get("image_url_list"):
            return found
    except Exception:
        pass
    return None


def _parse_douyin_image_browser(share_text, timeout_ms=18000):
    """Extract an image note from Douyin's rendered desktop page.

    The current note page often leaves ``_ROUTER_DATA`` empty and fetches the
    signed image URLs in JavaScript.  A short-lived headless Chromium session
    lets Douyin create its own request signatures, then we read only the
    full-size ``aweme_images`` elements from the DOM.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    url = _extract_share_url(share_text)
    browser = None
    try:
        with _DOUYIN_BROWSER_LOCK:
            with sync_playwright() as playwright:
                browser = launch_browser(playwright.chromium,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = browser.new_page(
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                try:
                    page.wait_for_function(
                        """() => Array.from(document.images).some(img => {
                          const src = img.currentSrc || img.src || "";
                          return src.includes("aweme_images");
                        })""",
                        timeout=timeout_ms,
                    )
                except Exception:
                    pass

                payload = page.evaluate(
                    """() => ({
                      pageUrl: location.href || "",
                      title: document.title || "",
                      description:
                        document.querySelector('meta[name="description"]')?.content ||
                        document.querySelector('meta[property="og:description"]')?.content || "",
                      images: Array.from(document.images).map(img => ({
                        src: img.currentSrc || img.src || "",
                        width: img.naturalWidth || 0,
                        height: img.naturalHeight || 0,
                        alt: img.alt || ""
                      }))
                    })"""
                )
                browser.close()
                browser = None

        if not isinstance(payload, dict):
            return None

        image_urls = []
        seen_assets = set()
        description_candidates = [
            str(payload.get("description") or "").strip(),
            str(payload.get("title") or "").removesuffix(" - 抖音").strip(),
        ]
        for image in payload.get("images") or []:
            if not isinstance(image, dict):
                continue
            src = str(image.get("src") or "").strip()
            if (
                not src.startswith(("http://", "https://"))
                or "douyinpic.com" not in urlsplit(src).netloc.lower()
                or "aweme_images" not in src
            ):
                continue
            asset_key = urlsplit(src).path.split("~", 1)[0]
            if asset_key in seen_assets:
                continue
            seen_assets.add(asset_key)
            image_urls.append(src)
            alt = str(image.get("alt") or "").strip()
            if alt:
                description_candidates.insert(0, alt)

        if not image_urls:
            return None

        page_url = str(payload.get("pageUrl") or url)
        match = re.search(r"/(?:note|slides)/(\d{15,})", urlsplit(page_url).path)
        aweme_id = match.group(1) if match else ""
        description = next((text for text in description_candidates if text), "抖音图文")
        nickname = ""
        digg_count = 0
        # The rendered note page currently puts author/date/like metadata into
        # the description suffix.  Recover those fields so image posts are not
        # stored with a blank author and zero likes when _ROUTER_DATA is empty.
        meta_match = re.search(
            r"\s*-\s*(?P<author>.+?)于\d{8}发布在抖音，已经收获了(?P<likes>[\d.万亿]+)个喜欢",
            description,
        )
        if meta_match:
            nickname = meta_match.group("author").strip()
            likes_text = meta_match.group("likes")
            try:
                multiplier = 10000 if "万" in likes_text else (100000000 if "亿" in likes_text else 1)
                digg_count = int(float(likes_text.rstrip("万亿")) * multiplier)
            except (TypeError, ValueError):
                digg_count = 0
        return {
            "image_url_list": image_urls,
            "desc": description,
            "nickname": nickname,
            "aweme_id": aweme_id,
            "digg_count": digg_count,
            "resolved_url": page_url,
        }
    except Exception:
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _parse_douyin_video_browser(share_text, timeout_ms=20000):
    """Read the signed video detail response produced by Douyin's own page.

    Douyin's desktop player now commonly uses DASH.  The first CDN request is
    often a video-only ``media-video-avc1`` stream; treating it as the finished
    MP4 makes frame extraction work but leaves Whisper with no audio track.
    The signed ``aweme/detail`` response also exposes ``play_addr_h264`` (or
    ``play_addr``), whose progressive MP4 contains both video and audio.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        return None

    url = _extract_share_url(share_text)
    browser = None
    detail_payload = {}
    try:
        with _DOUYIN_BROWSER_LOCK:
            with sync_playwright() as playwright:
                browser = launch_browser(playwright.chromium,
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                page = browser.new_page(
                    viewport={"width": 1280, "height": 900},
                    locale="zh-CN",
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                )

                def capture_detail(response):
                    if "/aweme/v1/web/aweme/detail/" not in response.url:
                        return
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    if isinstance(payload, dict):
                        detail_payload.update(payload)

                page.on("response", capture_detail)
                page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                # The detail request normally arrives within 2-5 seconds.  A
                # bounded poll keeps failure cheaper than the legacy 25s probe.
                for _ in range(max(1, timeout_ms // 500)):
                    if detail_payload.get("aweme_detail"):
                        break
                    page.wait_for_timeout(500)
                page_url = page.url
                page_title = page.title()
                browser.close()
                browser = None

        detail = detail_payload.get("aweme_detail") or {}
        if not isinstance(detail, dict):
            return None
        video = detail.get("video") or {}
        if not isinstance(video, dict):
            return None

        progressive_urls = []
        # Prefer H.264 for broad ffmpeg/Whisper compatibility.  These addresses
        # are progressive MP4s, unlike entries whose path contains media-video.
        for address_key in ("play_addr_h264", "play_addr", "download_addr"):
            address = video.get(address_key) or {}
            if not isinstance(address, dict):
                continue
            for candidate in address.get("url_list") or []:
                candidate = str(candidate or "").strip()
                if candidate.startswith(("http://", "https://")) and "media-video-" not in candidate:
                    progressive_urls.append(candidate)
            if progressive_urls:
                break
        if not progressive_urls:
            return None

        author_data = detail.get("author") or {}
        statistics = detail.get("statistics") or {}
        desc = str(detail.get("desc") or detail.get("item_title") or "").strip()
        if not desc:
            desc = str(page_title or "").removesuffix(" - 抖音").strip()
        return {
            "video_url": progressive_urls[0],
            "video_url_list": progressive_urls,
            "desc": desc,
            "nickname": str(author_data.get("nickname") or "").strip(),
            "aweme_id": str(detail.get("aweme_id") or ""),
            "digg_count": int(statistics.get("digg_count") or 0),
            "comment_count": int(statistics.get("comment_count") or 0),
            "share_count": int(statistics.get("share_count") or 0),
            "resolved_url": page_url,
        }
    except Exception:
        return None
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass


def _parse_douyin(share_text, retries=2):
    """使用本地网页解析器判断抖音帖类型，读取图集或视频信息。"""
    url = _extract_share_url(share_text)
    modal_video_url = _modal_id_video_url(url)
    prefer_image_first = bool(modal_video_url and "/video/" not in urlsplit(url).path)

    # Current Douyin mobile redirects still expose the canonical post route.
    # Use that before the legacy helper, whose old share page parser now often
    # misses video ids and unnecessarily falls through to an overseas service.
    redirect = _resolve_douyin_redirect(share_text)
    if redirect and redirect.get("kind") == "image":
        image_post = _parse_douyin_image_local(share_text)
        if not image_post:
            image_post = _parse_douyin_image_browser(share_text)
        if image_post:
            return image_post
    if redirect and redirect.get("kind") == "video":
        video_post = _parse_douyin_video_browser(share_text)
        if video_post:
            return video_post
        return {
            "video_url": "__browser__",
            "aweme_id": redirect.get("aweme_id", ""),
            "resolved_url": redirect.get("resolved_url", ""),
        }

    # friend/user/self?modal_id=... 既可能是视频也可能是图文；先保住图文帖，避免被视频解析器误判。
    if prefer_image_first:
        img = _parse_douyin_image_local(share_text)
        if img:
            return img

    # --- 第1层：内置解析器判定视频帖（强制国内直连，临时禁代理）---
    _saved_proxy = os.environ.get("DOUYIN_PROXY")
    try:
        os.environ["DOUYIN_PROXY"] = "none"  # 抖音自家域名国内直连，别走代理
        from media_services import DouyinDownloadService
        svc = DouyinDownloadService()
        local = None
        for candidate in [url, modal_video_url]:
            if not candidate:
                continue
            try:
                local = svc._resolve_share_url_locally(candidate)
                break
            except Exception:
                continue
        if local and (isinstance(local, dict) and local.get("aweme_id")):
            # 内置解析器能识别 = 视频帖，交给 _process_video_post 本地下载
            return {"video_url": "__local__", "aweme_id": local.get("aweme_id"),
                    "title": local.get("title", "")}
    except Exception:
        pass
    finally:
        if _saved_proxy is None:
            os.environ.pop("DOUYIN_PROXY", None)
        else:
            os.environ["DOUYIN_PROXY"] = _saved_proxy

    # --- 第2层：本地图文解析（国内直连）---
    if not prefer_image_first:
        img = _parse_douyin_image_local(share_text)
        if img:
            return img

    # Local-only distribution: do not send share URLs to third-party parsing gateways.
    return None


def _garbled_ratio(text: str) -> float:
    """统计文本中非正常字符的占比。返回 0.0~1.0，值越大越像乱码。
    正常字符包括：中英日韩文、数字、常见标点、空白、BMP表情符号。"""
    if not text or not text.strip():
        return 0.0
    sample = text[:500]
    valid = 0
    for ch in sample:
        cp = ord(ch)
        if (0x2000 <= cp <= 0xFFFF or         # CJK/符号/韩文/日文片假
            0x0020 <= cp <= 0x007E or         # ASCII可打印
            0x0009 <= cp <= 0x000D or         # 制表换行回车
            0x00A0 <= cp <= 0x00FF or         # Latin-1补充
            0x1000 <= cp <= 0x109F):
            valid += 1
        elif 0x0000 <= cp <= 0x0008:           # 控制字符
            valid += 1
        else:
            # 中文字常见区 0x4E00-0x9FFF 已在 0x2000-0xFFFF 内覆盖
            pass
    return 1.0 - valid / max(len(sample), 1)


def _analyze_video_subtitle(item_id: int, content: str):
    """后台分析视频字幕：判定是否含提示词、提取、分类、打分。失败不影响主流程。"""
    try:
        from analyzer import analyze_one, save_analysis, has_api_key
        if not has_api_key():
            return
        result = analyze_one(content, platform="抖音")
        conn = get_conn()
        save_analysis(conn, item_id, result)
        conn.close()
        return result
    except Exception:
        return None


def _subtitle_timeline(trans) -> tuple[str, list[dict], str]:
    """把 ASR 结果保留为可追溯到视频时间轴的文本和 JSON。"""
    entries = []
    lines = []
    for segment in (getattr(trans, "segments", None) or []):
        # 本地 ASR 常输出繁体，统一转简体后再入库/出报告。
        text = to_simplified((getattr(segment, "text", "") or "").strip())
        if not text:
            continue
        start_ms = int(getattr(segment, "start_ms", 0) or 0)
        end_ms = int(getattr(segment, "end_ms", start_ms) or start_ms)
        speaker_id = int(getattr(segment, "speaker_id", 0) or 0)
        start = start_ms / 1000
        end = end_ms / 1000
        def _tc(value):
            total_ms = max(0, int(round(value * 1000)))
            hours, remain = divmod(total_ms, 3_600_000)
            minutes, remain = divmod(remain, 60_000)
            seconds, millis = divmod(remain, 1000)
            return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"
        entries.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_timecode": _tc(start),
            "end_timecode": _tc(end),
            "speaker_id": speaker_id,
            "text": text,
        })
        lines.append(f"[{_tc(start)}-{_tc(end)}] {text}")
    timed_text = "\n".join(lines)
    return timed_text, entries, (getattr(trans, "srt_text", "") or "")


def _analyze_video_frames(
    item_id: int,
    title: str,
    subtitle: str,
    video_path: str,
    mode: str = "parse",
):
    """按全时间轴抽帧，并生成有证据时间码的解析或拉片报告。"""
    if not video_path or not os.path.exists(video_path):
        return [], [], [], "", "（没有可读取的视频文件，无法做关键帧分析。）", "", ""
    try:
        from crawler_bilibili import (
            _as_local_url,
            _extract_timeline_frames,
            _sha1,
            _timeline_manifest_text,
            _timeline_visual_analyze,
        )

        if mode == "reverse_prompt":
            key = f"douyin_reverse_{item_id}_{_sha1(video_path)}"
            frame_paths, frame_manifest = _extract_timeline_frames(
                video_path,
                key,
                max_frames=1,
                target_interval=999,
            )
            if not frame_paths:
                return [], [], [], "", "", "", "（未能从视频中提取首帧，无法反推提示词。）"
            first = frame_manifest[0] if frame_manifest else {}
            frame_text = "【首帧时间码】\n" + _timeline_manifest_text(frame_manifest, max_items=1)
            try:
                from reference_prompt import generate_dual_prompt_report

                reverse_prompt = generate_dual_prompt_report(
                    frame_paths[:1],
                    context=title or "抖音视频",
                    source_type="video_first_frame",
                    labels=[f"视频首帧（{first.get('timecode') or '起始画面'}）"],
                    max_images=1,
                )
            except Exception as e:
                reverse_prompt = f"（视频首帧提示词反推失败：{e}）"
            return (
                frame_paths,
                [_as_local_url(p) for p in frame_paths],
                frame_manifest,
                frame_text,
                "",
                "",
                reverse_prompt,
            )

        mode = "lapian" if mode == "lapian" else "parse"
        key = f"douyin_{mode}_{item_id}_{_sha1(video_path)}"
        frame_paths, frame_manifest = _extract_timeline_frames(
            video_path,
            key,
            max_frames=36 if mode == "lapian" else 24,
            target_interval=0.85 if mode == "lapian" else 1.25,
        )
        if not frame_paths:
            return [], [], [], "", "（未能从视频中抽取关键帧，当前仅保留字幕内容。）", "", ""
        desc = (
            f"{title or '抖音视频'}\n\n"
            f"任务模式：{'逐镜拉片' if mode == 'lapian' else '视频解析'}。"
            "请以帧号和时间码为唯一画面证据，字幕只辅助对齐台词。\n\n"
            f"带时间码字幕/音频转文本：\n{(subtitle or '')[:7000]}"
        )
        # OCR 与画面事实在同一次分组视觉取证中完成，避免旧流程逐图调用 20 次并产生重复转录。
        frame_text = "【帧号与时间码对照】\n" + _timeline_manifest_text(frame_manifest, max_items=60)
        visual, observations = _timeline_visual_analyze(
            frame_manifest,
            context=desc,
            frame_text=frame_text,
            mode=mode,
        )
        if observations:
            frame_text += "\n\n【逐帧画面文字/OCR证据】\n" + observations
        return (
            frame_paths,
            [_as_local_url(p) for p in frame_paths],
            frame_manifest,
            frame_text,
            visual,
            observations,
            "",
        )
    except Exception as e:
        return [], [], [], "", f"（视频画面分析失败：{e}）", "", ""


def _process_video_post(item_id, share_text, do_subtitle, mode="parse", parsed=None):
    """视频帖：下载视频 + 本地Whisper转字幕。"""
    from media_services import MultiPlatformDownloadService
    from media_services import DouyinDownloadService
    from media_services import AudioTranscriptionService
    import re

    # 抖音视频走国内 CDN 直连，强制禁代理下载（实测 ~5s，走代理反而慢/易失败）。
    # 仅当直连整体失败时，才允许回落到代理再试一次（覆盖极端网络环境）。
    _saved_proxy = os.environ.get("DOUYIN_PROXY")
    os.environ["DOUYIN_PROXY"] = "none"
    dl = MultiPlatformDownloadService()
    download_text = _download_share_text(share_text)
    browser_error = None
    try:
        resolved_media_url = str((parsed or {}).get("video_url") or "").strip()
        if resolved_media_url.startswith(("http://", "https://")):
            # The page-detail parser deliberately selects a progressive MP4
            # with audio, avoiding the browser probe's first DASH video-only
            # request that previously broke subtitle extraction.
            result = dl.download_douyin_from_resolved_media(download_text, resolved_media_url)
        else:
            with _DOUYIN_BROWSER_LOCK:
                result = DouyinDownloadService()._download_via_browser_fallback(
                    _extract_share_url(download_text)
                )
    except Exception as exc:
        browser_error = str(exc).strip()
        try:
            result = dl.download_from_text(download_text)
        except Exception as direct_exc:
            # Last resort: allow the configured proxy and the helper's full
            # provider chain. This path is intentionally after the local browser.
            _configure_douyin_proxy()
            try:
                result = MultiPlatformDownloadService().download_from_text(download_text)
            except Exception as proxy_exc:
                detail = browser_error or "浏览器未返回视频地址"
                raise RuntimeError(
                    f"抖音浏览器直连失败：{detail}；国内解析失败：{direct_exc}；"
                    f"代理兜底失败：{proxy_exc}"
                ) from proxy_exc
            finally:
                if _saved_proxy is None:
                    os.environ.pop("DOUYIN_PROXY", None)
                else:
                    os.environ["DOUYIN_PROXY"] = _saved_proxy
        else:
            if _saved_proxy is None:
                os.environ.pop("DOUYIN_PROXY", None)
            else:
                os.environ["DOUYIN_PROXY"] = _saved_proxy
    else:
        if _saved_proxy is None:
            os.environ.pop("DOUYIN_PROXY", None)
        else:
            os.environ["DOUYIN_PROXY"] = _saved_proxy
    video_path = result.local_path
    parsed = parsed or {}
    parsed_desc = (parsed.get("desc") or "").strip()
    title = (parsed_desc.splitlines()[0][:100] if parsed_desc else "") or result.title or "抖音视频"
    author = parsed.get("nickname") or result.author or ""

    subtitle = ""
    audio_path = ""
    subtitle_error = ""
    subtitle_garbled = False
    asr_completed = False
    subtitle_timed = ""
    subtitle_segments = []
    subtitle_srt = ""
    if mode != "reverse_prompt" and do_subtitle and video_path and os.path.exists(video_path):
        try:
            asr = AudioTranscriptionService()
            prepared, trans = asr.transcribe_source(video_path)
            asr_completed = True
            subtitle = to_simplified(trans.text or "")
            audio_path = prepared.audio_path or ""
            subtitle_timed, subtitle_segments, subtitle_srt = _subtitle_timeline(trans)
        except Exception as e:
            subtitle_error = f"字幕提取失败：{str(e).strip()[:500]}"
        if asr_completed and not subtitle and not subtitle_error:
            subtitle_error = "本地ASR已完成，但未检测到可识别的人声或台词"
        # 乱码检测 + AI 纠错
        if subtitle and _garbled_ratio(subtitle) > 0.30:
            try:
                from analyzer import fix_garbled_text, has_api_key
                if has_api_key():
                    fixed = fix_garbled_text(subtitle)
                    if fixed and _garbled_ratio(fixed) < _garbled_ratio(subtitle):
                        subtitle = fixed
                        subtitle_garbled = False
                    else:
                        subtitle_garbled = True
                else:
                    subtitle_garbled = True
                    subtitle += "\n（字幕可能存在识别乱码，可安装本地模型后重新解析以辅助修正）"
            except Exception:
                subtitle_garbled = True

    transcript_for_analysis = subtitle_timed or subtitle
    frame_paths, frame_urls, frame_manifest, frame_text, visual_analysis, visual_observations, reverse_prompt_analysis = _analyze_video_frames(
        item_id=item_id,
        title=title,
        subtitle=transcript_for_analysis,
        video_path=video_path,
        mode=mode,
    )

    parts = [f"【标题】{title}"]
    if author:
        parts.append(f"【作者】{author}")
    if mode != "reverse_prompt" and subtitle:
        parts.append(f"【字幕】（来源：本地ASR，带时间码）\n{subtitle_timed or subtitle}")
    elif mode != "reverse_prompt":
        parts.append(f"【字幕】\n（{subtitle_error or '未提取到字幕'}）")
    if frame_text and mode == "reverse_prompt":
        parts.append(f"【首帧信息】\n{frame_text}")
    elif frame_text:
        parts.append(f"【视频图文信息提取】\n{frame_text}")
    if visual_analysis:
        heading = "拉片分析" if mode == "lapian" else "画面内容分析"
        parts.append(f"【{heading}】\n{visual_analysis}")
    if reverse_prompt_analysis:
        parts.append(f"【文生图/首帧图反推提示词】\n{reverse_prompt_analysis}")
    content = "\n\n".join(parts)

    share_url = result.share_url or share_text
    resolved_result_url = (
        getattr(result, "resolved_url", None)
        or getattr(result, "parser_url", None)
        or (parsed or {}).get("resolved_url")
        or share_url
    )
    m = re.search(r"video/(\d+)", resolved_result_url)
    video_id = m.group(1) if m else f"dy_{item_id}"

    raw = {
        "video_path": video_path,
        "video_url": (parsed or {}).get("video_url") or "",
        "audio_path": audio_path,
        "share_url": share_url,
        "kind": "video",
        "frame_paths": frame_paths,
        "image_paths": frame_paths,
        "image_urls": frame_urls,
        "frame_manifest": frame_manifest,
        "frame_text_extraction": frame_text,
        "visual_analysis": visual_analysis,
        "visual_observations": visual_observations,
        "analysis_mode": mode,
        "reverse_prompt_analysis": reverse_prompt_analysis,
        "subtitle_source": "本地ASR" if asr_completed else "",
        "asr_completed": asr_completed,
        "subtitle_error": subtitle_error,
        "subtitle_segments": subtitle_segments,
        "subtitle_srt": subtitle_srt,
    }
    _update_item(
        item_id, platform_id=video_id, title=title, author_name=author,
        content=content, source_url=share_url,
        like_count=(parsed or {}).get("digg_count") or 0,
        comment_count=(parsed or {}).get("comment_count") or 0,
        share_count=(parsed or {}).get("share_count") or 0,
        raw_json=json.dumps(raw, ensure_ascii=False),
        content_type="提示词反推" if mode == "reverse_prompt" else ("拉片" if mode == "lapian" else "视频"), status="完成",
    )

    # Only classify source text; generated visual prose is not a source prompt.
    if mode != "reverse_prompt" and (subtitle or frame_text) and not subtitle_garbled:
        _analyze_video_subtitle(item_id, f'【标题】{title}\n{transcript_for_analysis}\n{frame_text}')


def _process_image_post(item_id, share_text, parsed, mode="parse"):
    """图文帖：下载图片 + 抓文案 + 本地模型识图提取提示词。"""
    import re
    import requests

    author = parsed.get("nickname") or ""
    desc = parsed.get("desc") or ""
    # Remove the SEO suffix appended by the rendered share page.  Keeping it
    # made short captions turn into titles like "... - 作者于20260516发布".
    desc = re.sub(
        r"\s*-\s*.+?于\d{8}发布在抖音，已经收获了[\d.万亿]+个喜欢，来抖音，记录美好生活！?\s*$",
        "",
        desc,
        flags=re.S,
    ).strip()
    title_line = (desc or "抖音图文").split("\n")[0].strip()
    sentence = re.match(r"^(.+?[。！？?!])", title_line)
    title = ((sentence.group(1) if sentence else title_line) or "抖音图文")[:80]
    image_urls = parsed.get("image_url_list") or []
    aweme_id = str(parsed.get("aweme_id") or f"dy_{item_id}")

    # 下载图片到本地（抖音图床在国内，直连最稳，不走代理）
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "douyin_images", aweme_id)
    os.makedirs(out_dir, exist_ok=True)
    s = _direct_session()
    img_headers = {"User-Agent": _MOBILE_UA, "Referer": "https://www.douyin.com/"}
    local_images = []
    for i, iu in enumerate(image_urls, 1):
        try:
            resp = s.get(iu, headers=img_headers, timeout=30)
            if resp.status_code == 200:
                ext = ".jpg"
                p = os.path.join(out_dir, f"{i:02d}{ext}")
                with open(p, "wb") as f:
                    f.write(resp.content)
                local_images.append(p)
        except Exception:
            continue

    share_url = re.search(r"https?://\S+", share_text)
    share_url = share_url.group(0) if share_url else share_text

    def _base_parts():
        parts = [f"【标题】{title}"]
        if author:
            parts.append(f"【作者】{author}")
        if desc:
            parts.append(f"【文案】{desc}")
        return parts

    raw = {"image_paths": local_images, "share_url": share_url,
           "image_count": len(local_images), "kind": "image", "desc": desc,
           "ocr_done": False}

    parts = _base_parts()
    ocr_prompt = ""
    ocr_error = ""
    if mode != "reverse_prompt" and local_images:
        try:
            from analyzer import extract_prompt_from_images
            ocr_prompt = extract_prompt_from_images(local_images, desc=desc, max_images=20) or ""
        except Exception as e:
            ocr_error = f"图片图文信息提取失败：{e}"
    if ocr_prompt.strip():
        parts.append("【图片提示词】\n" + ocr_prompt.strip())
        raw["ocr_done"] = True
        raw["image_text_extraction"] = ocr_prompt.strip()
    elif ocr_error:
        parts.append("【图片提示词】\n（" + ocr_error + "）")
        raw["ocr_error"] = ocr_error

    reverse_prompt = ""
    if mode == "reverse_prompt" and local_images:
        try:
            from reference_prompt import generate_dual_prompt_report

            reverse_prompt = generate_dual_prompt_report(
                local_images,
                context=f"{title}\n{desc[:1200]}",
                source_type="image",
                labels=[f"抖音图文参考图{i}" for i in range(1, len(local_images) + 1)],
                max_images=8,
            )
        except Exception as e:
            reverse_prompt = f"（图文参考图提示词反推失败：{e}）"
    elif mode == "reverse_prompt":
        reverse_prompt = "（没有下载到可用图片，无法反推提示词。）"
    if reverse_prompt:
        parts.append("【文生图/首帧图反推提示词】\n" + reverse_prompt)
        raw["reverse_prompt_analysis"] = reverse_prompt

    raw["analysis_mode"] = mode
    _update_item(
        item_id, platform_id=aweme_id, title=title, author_name=author,
        content="\n\n".join(parts), source_url=share_url,
        like_count=parsed.get("digg_count") or 0,
        raw_json=json.dumps(raw, ensure_ascii=False),
        content_type="提示词反推" if mode == "reverse_prompt" else "图文", status="完成",
    )

    if mode != "reverse_prompt" and ocr_prompt.strip():
        try:
            from analyzer import analyze_one, save_analysis
            result = analyze_one("\n\n".join(parts), platform="抖音")
            conn = get_conn()
            save_analysis(conn, item_id, result)
            conn.close()
        except Exception:
            pass


def run_ocr_for_item(item_id) -> dict:
    """对一条已完成的图文帖手动触发识图提取提示词。前端「识图提取」按钮调用。
    返回 {ok, prompt_text} 或 {ok:False, error}。"""
    conn = get_conn()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute("SELECT content, raw_json FROM items WHERE id=?", (item_id,)).fetchone()
    conn.close()
    if not row:
        return {"ok": False, "error": "记录不存在"}
    try:
        raw = json.loads(row["raw_json"] or "{}")
    except Exception:
        raw = {}
    local_images = [p for p in (raw.get("image_paths") or []) if p and os.path.exists(p)]
    if not local_images:
        return {"ok": False, "error": "没有可识别的本地图片（可能已被清理，请重新解析）"}

    try:
        from analyzer import extract_prompt_from_images, analyze_one, save_analysis
        ocr_prompt = extract_prompt_from_images(local_images, desc=raw.get("desc", "")) or ""
    except Exception as e:
        return {"ok": False, "error": f"识图失败: {e}"}

    if not ocr_prompt.strip():
        return {"ok": True, "prompt_text": "", "msg": "图片里没识别到提示词"}

    # 回填 content：去掉旧的【图片提示词】段再追加新的
    base = row["content"] or ""
    if "【图片提示词】" in base:
        base = base.split("【图片提示词】", 1)[0].rstrip()
    new_content = base + "\n\n【图片提示词】\n" + ocr_prompt
    raw["ocr_done"] = True
    _update_item(item_id, content=new_content, raw_json=json.dumps(raw, ensure_ascii=False))

    # 顺带做一次 AI 分析（判定/分类/打分）
    try:
        result = analyze_one(new_content, platform="抖音")
        conn = get_conn()
        save_analysis(conn, item_id, result)
        conn.close()
    except Exception:
        pass

    return {"ok": True, "prompt_text": ocr_prompt}


def process_douyin_link(share_text, do_subtitle=True, mode="parse") -> dict:
    """
    入口：贴链接 → 立即入库占位 → 后台线程处理。
    立即返回 item_id，前端轮询/刷新看结果。
    """
    if not share_text or not share_text.strip():
        return {"ok": False, "error": "请粘贴抖音分享链接"}
    command = extract_douyin_command(share_text)
    if command and not first_url(share_text):
        return {
            "ok": False,
            "error": (
                f"已识别到抖音口令（{command}），但它是抖音 App 专用口令，不是网页链接。"
                "请先在抖音 App 粘贴口令并打开作品，再点「分享 → 复制链接」，"
                "将包含 v.douyin.com 的完整分享文案提交。"
            ),
        }
    share_text = normalize_douyin_share_text(share_text)
    if mode not in ("parse", "lapian", "reverse_prompt"):
        mode = "parse"
    # Mobile redirect normally resolves in about one second and gives us the
    # canonical aweme id before the async worker starts. This closes the race
    # where two short URLs for one post created competing placeholder rows.
    redirect = _resolve_douyin_redirect(share_text) or {}
    canonical_id = str(redirect.get("aweme_id") or "")
    normalized_url = _extract_share_url(share_text)
    job_key = canonical_id or hashlib.sha1(normalized_url.encode("utf-8")).hexdigest()[:20]
    with _ACTIVE_JOB_LOCK:
        active_item_id = _ACTIVE_JOBS.get(job_key)
        if active_item_id:
            return {
                "ok": True,
                "item_id": active_item_id,
                "status": "处理中",
                "mode": mode,
                "already_processing": True,
            }
        item_id = create_placeholder(share_text, mode=mode, canonical_id=canonical_id)
        if not item_id:
            return {"ok": False, "error": "创建任务失败"}
        _ACTIVE_JOBS[job_key] = item_id
        t = threading.Thread(
            target=_run_process_job,
            args=(job_key, item_id, share_text, do_subtitle, mode),
            daemon=True,
        )
        t.start()
    return {"ok": True, "item_id": item_id, "status": "处理中", "mode": mode}


if __name__ == "__main__":
    if len(sys.argv) > 1:
        r = process_douyin_link(sys.argv[1])
        print("已提交后台:", r)
        import time
        # 命令行测试时等它跑完
        if r.get("ok"):
            iid = r["item_id"]
            for _ in range(120):
                time.sleep(3)
                conn = get_conn()
                row = conn.execute("SELECT status,title FROM items WHERE id=?", (iid,)).fetchone()
                conn.close()
                print("  状态:", row[0], "|", row[1][:30])
                if row[0] in ("完成", "失败"):
                    break
    else:
        print('用法: py crawler_douyin.py "抖音分享链接"')
