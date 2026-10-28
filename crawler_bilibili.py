# -*- coding: utf-8 -*-
"""
B站链接处理模块。

目标：给链接分析首页补齐接近抖音解析的素材能力：
- 视频：解析元信息、下载视频/音频、合并视频、转字幕、抽关键帧、画面内容分析、报告/素材下载。
- 图文：解析正文/图片、下载图片、识图梳理图文内容、报告/图片包下载。

不改原有 server.py/link_extractor.py/link_pipeline.py，路由由运行包装器注册。
"""

import base64
import datetime
import hashlib
import io
import json
import os
from browser_support import launch_browser
import re
import subprocess
import threading
import queue
import time
import zipfile
from pathlib import Path
from urllib.parse import urlsplit

import requests
from flask import jsonify, redirect, request, send_file

from database import get_conn, insert_item
from link_pipeline import LINK_SOURCE_TAG
from text_tools import to_simplified


ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = ROOT / "douyin_images"
BILI_MEDIA_ROOT = ROOT / "bilibili_media"
LOG_PATH = ROOT / "bilibili_current.log"

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
)

# 视觉模型被拒（密钥失效/网关不通）后，同一条视频剩余帧不再重试。
_VISION_BLOCK_REASON = ""


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _log(msg: str):
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{_now_iso()}] {msg}\n")
    except Exception:
        pass


def _sha1(text: str) -> str:
    return hashlib.sha1((text or "").encode("utf-8")).hexdigest()[:16]


def _first_url(text: str) -> str:
    m = re.search(r"https?://\S+", text or "")
    if not m:
        return (text or "").strip()
    return m.group(0).rstrip(".,);，。；）")


def is_bilibili_url(text: str) -> bool:
    host = (urlsplit(_first_url(text)).hostname or "").lower()
    return host == "b23.tv" or host.endswith(".b23.tv") or "bilibili.com" in host


def is_douyin_url(text: str) -> bool:
    host = (urlsplit(_first_url(text)).hostname or "").lower()
    return "douyin.com" in host


def _headers(url: str):
    return {
        "User-Agent": UA,
        "Referer": url if "bilibili.com" in url else "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "*/*",
    }


def _safe_name(name: str, fallback="bilibili") -> str:
    name = (name or fallback).strip()
    return "".join(c for c in name if c not in '\\/:*?"<>|')[:60] or fallback


def _as_local_url(path: str) -> str:
    p = Path(path)
    try:
        key = p.parent.name
        return f"/douyin_images/{key}/{p.name}"
    except Exception:
        return ""


def _update_item(item_id, **fields):
    conn = get_conn()
    sets = ", ".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE items SET {sets} WHERE id=?", list(fields.values()) + [item_id])
    conn.commit()
    conn.close()


def _progress(item_id: int, step: str, detail: str = ""):
    text = f"当前步骤：{step}"
    if detail:
        text += f"\n{detail}"
    text += "\n\n视频类解析通常需要下载音视频、转字幕、抽关键帧和画面分析；长视频的本地 ASR 会比较慢。"
    try:
        _update_item(
            item_id,
            title=f"B站解析中：{step}",
            content=text,
            status="处理中",
        )
    except Exception:
        pass


def create_placeholder(share_text: str, mode: str = "parse") -> int:
    url = _first_url(share_text)
    key = _sha1(url)
    is_lapian = mode == "lapian"
    is_reverse = mode == "reverse_prompt"
    conn = get_conn()
    item = {
        "platform": "B站",
        "platform_id": f"bili_pending_{key}",
        "content_type": "提示词反推" if is_reverse else ("拉片" if is_lapian else "链接解析"),
        "author_name": "",
        "author_id": "",
        "publish_time": _now_iso(),
        "title": "B站首帧反推中…" if is_reverse else ("B站拉片中…" if is_lapian else "B站解析中…"),
        "content": (
            "正在提取 B站视频首帧，并反推 Midjourney 与 GPT 两套文生图提示词。"
            if is_reverse else
            "正在后台解析 B站视频并生成拉片稿：会下载音视频、转字幕、抽关键帧，再按视听语言拆解镜头。"
            if is_lapian else
            "正在后台解析 B站链接，视频会下载音频/视频、转字幕、抽关键帧并分析画面内容。"
        ),
        "media_urls": "",
        "like_count": 0,
        "comment_count": 0,
        "share_count": 0,
        "matched_keyword": LINK_SOURCE_TAG,
        "source_url": url,
        "raw_json": json.dumps({"share_text": share_text, "kind": "processing", "analysis_mode": mode}, ensure_ascii=False),
        "crawled_at": _now_iso(),
        "status": "处理中",
    }
    insert_item(conn, item)
    row = conn.execute(
        "SELECT id FROM items WHERE platform=? AND platform_id=?",
        ("B站", f"bili_pending_{key}"),
    ).fetchone()
    conn.close()
    return row[0] if row else None


def _fetch_html(url: str) -> tuple[str, str]:
    s = requests.Session()
    s.trust_env = True
    r = s.get(url, headers=_headers(url), timeout=30, allow_redirects=True)
    r.raise_for_status()
    return r.text, r.url


def _json_from_initial_state(html: str) -> dict:
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", html, re.S)
    if not m:
        return {}
    return json.loads(m.group(1))


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def _fmt_date(ts) -> str:
    try:
        if ts:
            return datetime.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d")
    except Exception:
        pass
    return ""


def _extract_bvid(url: str, html: str = "") -> str:
    m = re.search(r"/video/(BV[0-9A-Za-z]+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r'"bvid"\s*:\s*"(BV[0-9A-Za-z]+)"', html or "")
    return m.group(1) if m else ""


def parse_video_meta(url: str) -> dict:
    html, final_url = _fetch_html(url)
    state = _json_from_initial_state(html)
    video = state.get("videoData") or {}
    owner = video.get("owner") or state.get("upData") or {}
    stat = video.get("stat") or {}
    bvid = video.get("bvid") or state.get("bvid") or _extract_bvid(final_url, html)
    cid = video.get("cid") or state.get("cid")
    aid = video.get("aid") or state.get("aid")
    title = video.get("title") or ""
    desc = video.get("desc") or ""
    tags = []
    for item in state.get("tags") or []:
        if isinstance(item, dict) and item.get("tag_name"):
            tags.append(item["tag_name"])
    return {
        "kind": "video",
        "url": final_url,
        "bvid": bvid,
        "cid": cid,
        "aid": aid,
        "title": title,
        "desc": desc,
        "author": owner.get("name") or "",
        "author_id": str(owner.get("mid") or ""),
        "publish_time": _fmt_date(video.get("pubdate") or video.get("ctime")),
        "duration": int(video.get("duration") or 0),
        "cover_url": _norm_url(video.get("pic") or ""),
        "tags": tags,
        "stat": stat,
    }


def parse_image_text(url: str) -> dict:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = launch_browser(p.chromium,headless=True)
        ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900})
        page = ctx.new_page()
        page.goto(url, timeout=45000, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        data = page.evaluate(
            """() => {
                const meta = (name) =>
                    document.querySelector(`meta[property="${name}"]`)?.content ||
                    document.querySelector(`meta[name="${name}"]`)?.content || "";
                const pick = (selectors) => {
                    for (const sel of selectors) {
                        const el = document.querySelector(sel);
                        const txt = (el?.getAttribute("title") || el?.innerText || "").trim();
                        if (txt) return txt;
                    }
                    return "";
                };
                const contentEl =
                    document.querySelector("article") ||
                    document.querySelector(".article-content") ||
                    document.querySelector(".opus-module-content") ||
                    document.querySelector(".dyn-card-opus__summary") ||
                    document.querySelector("main") ||
                    document.body;
                const imgs = Array.from(document.querySelectorAll("article img, main img, .opus-module-content img, .bili-album__watch__content img"))
                    .map(e => e.getAttribute("data-src") || e.getAttribute("src") || "")
                    .filter(Boolean);
                return {
                    title: pick(["h1", ".title", ".article-title"]) || meta("og:title") || document.title || "",
                    author: pick([".up-name", ".username", ".name", ".opus-module-author__name"]) || "",
                    desc: meta("description") || meta("og:description") || "",
                    content: (contentEl?.innerText || document.body.innerText || "").trim(),
                    image_urls: imgs
                };
            }"""
        ) or {}
        final_url = page.url
        browser.close()

    title = (data.get("title") or "").replace("_哔哩哔哩_bilibili", "").strip()
    content = (data.get("content") or data.get("desc") or "").strip()
    return {
        "kind": "image_text",
        "url": final_url,
        "title": title or "B站图文",
        "author": data.get("author") or "",
        "publish_time": "",
        "desc": data.get("desc") or "",
        "content": content,
        "image_urls": [_norm_url(u) for u in data.get("image_urls") or []],
    }


def _download_file(url: str, path: Path, referer: str, timeout=120) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = _headers(referer)
    h["Referer"] = referer
    with requests.get(url, headers=h, timeout=timeout, stream=True) as r:
        r.raise_for_status()
        with open(path, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 512):
                if chunk:
                    f.write(chunk)
    return str(path)


def _download_images(image_urls, key: str, referer: str, limit=20) -> list[str]:
    out_dir = MEDIA_ROOT / key
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for idx, url in enumerate((image_urls or [])[:limit], 1):
        try:
            clean = _norm_url(url).split("@", 1)[0]
            ext = os.path.splitext(urlsplit(clean).path)[1] or ".jpg"
            if ext.lower() not in (".jpg", ".jpeg", ".png", ".webp"):
                ext = ".jpg"
            paths.append(_download_file(clean, out_dir / f"{idx:02d}{ext}", referer, timeout=40))
        except Exception:
            continue
    return paths


def _pick_streams(play_data: dict) -> tuple[dict | None, dict | None, dict | None]:
    dash = (play_data or {}).get("dash") or {}
    videos = dash.get("video") or []
    audios = dash.get("audio") or []
    videos = sorted(videos, key=lambda x: (x.get("height") or 0, x.get("bandwidth") or 0), reverse=True)
    audios = sorted(audios, key=lambda x: x.get("bandwidth") or 0, reverse=True)
    durl = ((play_data or {}).get("durl") or [None])[0]
    return (videos[0] if videos else None, audios[0] if audios else None, durl)


def _playurl(meta: dict) -> dict:
    r = requests.get(
        "https://api.bilibili.com/x/player/playurl",
        params={"bvid": meta["bvid"], "cid": meta["cid"], "fnval": "4048", "fourk": "1"},
        headers=_headers(meta["url"]),
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"B站播放流接口失败：{data.get('message') or data.get('code')}")
    return data.get("data") or {}


def _run_ffmpeg(args: list[str]):
    p = subprocess.run(
        ["ffmpeg", "-y", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout or "ffmpeg failed")[-1000:])


def _video_duration_seconds(video_path: str) -> float:
    if not video_path or not os.path.exists(video_path):
        return 0.0
    try:
        p = subprocess.run(
            [
                "ffprobe",
                "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if p.returncode == 0:
            return max(0.0, float((p.stdout or "").strip() or 0))
    except Exception:
        pass
    return 0.0


def _download_video_audio(meta: dict, key: str) -> dict:
    media_dir = BILI_MEDIA_ROOT / key
    media_dir.mkdir(parents=True, exist_ok=True)
    play = _playurl(meta)
    v, a, durl = _pick_streams(play)
    result = {"play_quality": play.get("quality"), "video_stream_path": "", "audio_stream_path": ""}
    if v and a:
        v_url = v.get("baseUrl") or v.get("base_url")
        a_url = a.get("baseUrl") or a.get("base_url")
        v_path = media_dir / "video_stream.m4s"
        a_path = media_dir / "audio.m4a"
        _download_file(v_url, v_path, meta["url"])
        _download_file(a_url, a_path, meta["url"])
        merged = media_dir / "merged.mp4"
        _run_ffmpeg(["-i", str(v_path), "-i", str(a_path), "-c", "copy", str(merged)])
        # The standalone video stream is only an intermediate file. Keep the
        # merged video and audio track, but do not accumulate duplicate video
        # payloads after a successful merge.
        if merged.is_file() and merged.stat().st_size > 0:
            v_path.unlink(missing_ok=True)
        result.update({
            "video_path": str(merged),
            "audio_path": str(a_path),
            "video_stream_path": "",
            "audio_stream_path": str(a_path),
        })
        return result

    if durl and durl.get("url"):
        raw_mp4 = media_dir / "video.mp4"
        _download_file(durl.get("url"), raw_mp4, meta["url"])
        audio = media_dir / "audio.m4a"
        try:
            _run_ffmpeg(["-i", str(raw_mp4), "-vn", "-acodec", "copy", str(audio)])
        except Exception:
            audio = ""
        result.update({"video_path": str(raw_mp4), "audio_path": str(audio) if audio else ""})
        return result

    raise RuntimeError("没有拿到可下载的视频/音频流")


def _fetch_official_subtitle(meta: dict) -> tuple[str, str]:
    try:
        r = requests.get(
            "https://api.bilibili.com/x/player/v2",
            params={"bvid": meta["bvid"], "cid": meta["cid"]},
            headers=_headers(meta["url"]),
            timeout=20,
        )
        data = r.json().get("data") or {}
        subtitles = ((data.get("subtitle") or {}).get("subtitles") or [])
        if not subtitles:
            return "", ""
        sub_url = subtitles[0].get("subtitle_url") or ""
        sub_url = _norm_url(sub_url)
        if not sub_url:
            return "", ""
        sr = requests.get(sub_url, headers=_headers(meta["url"]), timeout=20)
        body = sr.json().get("body") or []
        lines = []
        for item in body:
            text = (item.get("content") or "").strip()
            if text:
                lines.append(text)
        return "\n".join(lines), "B站官方字幕"
    except Exception:
        return "", ""


def _asr_subtitle(video_path: str) -> tuple[str, str, str]:
    try:
        from crawler_douyin import _garbled_ratio

        from media_services import AudioTranscriptionService

        asr = AudioTranscriptionService()
        prepared, trans = asr.transcribe_source(video_path)
        subtitle = to_simplified(trans.text or "")
        if subtitle and _garbled_ratio(subtitle) > 0.30:
            try:
                from analyzer import fix_garbled_text, has_api_key
                if has_api_key():
                    fixed = fix_garbled_text(subtitle)
                    if fixed:
                        subtitle = fixed
            except Exception:
                pass
        return subtitle, prepared.audio_path or "", "本地ASR"
    except Exception as e:
        return f"（字幕生成失败：{e}）", "", "失败"


def _extract_frames(video_path: str, key: str, max_frames=8) -> list[str]:
    if not video_path or not os.path.exists(video_path):
        return []
    frame_dir = MEDIA_ROOT / key
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("frame_*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass

    duration = _video_duration_seconds(video_path)
    if duration > 0:
        count = max(1, min(max_frames, int(duration) if duration >= 1 else 1))
        if count == 1:
            timestamps = [max(0.0, duration / 2)]
        else:
            edge = min(1.5, max(0.3, duration * 0.06))
            start = min(edge, max(0.0, duration - 0.2))
            end = max(start, duration - edge)
            timestamps = [
                start + (end - start) * i / (count - 1)
                for i in range(count)
            ]
        for idx, ts in enumerate(timestamps, 1):
            out = frame_dir / f"frame_{idx:02d}.jpg"
            try:
                _run_ffmpeg([
                    "-ss", f"{ts:.2f}",
                    "-i", video_path,
                    "-frames:v", "1",
                    "-vf", "scale=960:-1",
                    str(out),
                ])
            except Exception:
                continue
        paths = [str(p) for p in sorted(frame_dir.glob("frame_*.jpg"))]
        if paths:
            return paths

    pattern = frame_dir / "frame_%02d.jpg"
    try:
        _run_ffmpeg([
            "-i", video_path,
            "-vf", "fps=1/5,scale=960:-1",
            "-frames:v", str(max_frames),
            str(pattern),
        ])
    except Exception:
        return []
    return [str(p) for p in sorted(frame_dir.glob("frame_*.jpg"))]


def _format_timecode(seconds: float, with_ms: bool = True) -> str:
    """把秒数格式化为稳定的视频时间码。"""
    total_ms = max(0, int(round(float(seconds or 0) * 1000)))
    hours, remain = divmod(total_ms, 3_600_000)
    minutes, remain = divmod(remain, 60_000)
    secs, millis = divmod(remain, 1000)
    if with_ms:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _detect_scene_boundaries(video_path: str, threshold: float = 0.22) -> list[float]:
    """用 ffmpeg 场景分数找硬切点；无硬切的连续镜头会自然返回空列表。"""
    if not video_path or not os.path.exists(video_path):
        return []
    try:
        p = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", video_path,
                "-vf", f"select='gt(scene,{threshold:.2f})',showinfo",
                "-f", "null", "-",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output = (p.stderr or "") + "\n" + (p.stdout or "")
        values = [float(x) for x in re.findall(r"pts_time:([0-9]+(?:\.[0-9]+)?)", output)]
    except Exception:
        return []

    cleaned = []
    for value in sorted(values):
        if value <= 0.08:
            continue
        if cleaned and value - cleaned[-1] < 0.35:
            continue
        cleaned.append(round(value, 3))
    return cleaned


def _extract_timeline_frames(
    video_path: str,
    key: str,
    max_frames: int = 32,
    target_interval: float = 1.0,
) -> tuple[list[str], list[dict]]:
    """按完整时间轴抽取证据帧，并给每帧保留真实时间码和所属镜头区间。

    这里不把“逐帧”误解成把 24/30fps 的每一帧都送入视觉模型。程序会同时
    保留硬切边界和约每秒一个连续采样点，使短视频从头到尾都有证据覆盖。
    """
    if not video_path or not os.path.exists(video_path):
        return [], []
    duration = _video_duration_seconds(video_path)
    if duration <= 0:
        paths = _extract_frames(video_path, key, max_frames=max_frames)
        manifest = [
            {
                "index": idx,
                "time_sec": None,
                "timecode": "时间码未知",
                "shot_id": None,
                "shot_start_sec": None,
                "shot_end_sec": None,
                "shot_range": "时间码未知",
                "path": path,
                "url": _as_local_url(path),
            }
            for idx, path in enumerate(paths, 1)
        ]
        return paths, manifest

    max_frames = max(1, min(int(max_frames or 32), 60))
    # 反推只需要定场首帧，不必扫描整段视频寻找转场。
    cut_points = [] if max_frames == 1 else [x for x in _detect_scene_boundaries(video_path) if 0.12 < x < duration - 0.12]
    boundaries = [0.0, *cut_points, duration]

    # 连续采样负责覆盖无硬切的表演、屏录、字幕和教程视频；镜头中点负责保住短镜头。
    if max_frames == 1:
        candidates = [round(min(0.25, max(0.0, duration / 2)), 3)]
    else:
        interval = max(float(target_interval or 1.0), duration / max_frames)
        periodic = []
        cursor = min(0.25, duration / 2)
        while cursor < duration:
            periodic.append(min(cursor, max(0.0, duration - 0.08)))
            cursor += interval
        shot_midpoints = [
            (boundaries[i] + boundaries[i + 1]) / 2
            for i in range(len(boundaries) - 1)
            if boundaries[i + 1] - boundaries[i] >= 0.08
        ]
        candidates = sorted({round(x, 3) for x in [*periodic, *shot_midpoints, max(0.0, duration - 0.12)]})
    if len(candidates) > max_frames:
        # 等距保留首尾和全时轴覆盖；硬切中点会通过最近采样点被覆盖。
        picks = {
            round(i * (len(candidates) - 1) / (max_frames - 1))
            for i in range(max_frames)
        }
        candidates = [candidates[i] for i in sorted(picks)]

    frame_dir = MEDIA_ROOT / key
    frame_dir.mkdir(parents=True, exist_ok=True)
    for old in frame_dir.glob("frame_*.jpg"):
        try:
            old.unlink()
        except Exception:
            pass

    paths = []
    manifest = []
    for idx, ts in enumerate(candidates, 1):
        shot_index = 0
        for i in range(len(boundaries) - 1):
            if boundaries[i] <= ts < boundaries[i + 1] or (i == len(boundaries) - 2 and ts <= boundaries[i + 1]):
                shot_index = i
                break
        shot_start = boundaries[shot_index]
        shot_end = boundaries[shot_index + 1]
        safe_tc = _format_timecode(ts).replace(":", "-").replace(".", "-")
        out = frame_dir / f"frame_{idx:03d}_{safe_tc}.jpg"
        try:
            _run_ffmpeg([
                "-ss", f"{ts:.3f}",
                "-i", video_path,
                "-frames:v", "1",
                "-vf", "scale=960:-1",
                str(out),
            ])
        except Exception:
            continue
        if not out.exists():
            continue
        path = str(out)
        paths.append(path)
        manifest.append({
            "index": len(manifest) + 1,
            "time_sec": round(ts, 3),
            "timecode": _format_timecode(ts),
            "shot_id": shot_index + 1,
            "shot_start_sec": round(shot_start, 3),
            "shot_end_sec": round(shot_end, 3),
            "shot_range": f"{_format_timecode(shot_start)}-{_format_timecode(shot_end)}",
            "path": path,
            "url": _as_local_url(path),
        })
    return paths, manifest


def _timeline_manifest_text(frame_manifest: list[dict], max_items: int = 60) -> str:
    lines = []
    for entry in (frame_manifest or [])[:max_items]:
        lines.append(
            f"帧{int(entry.get('index') or 0):03d}｜{entry.get('timecode') or '时间码未知'}｜"
            f"所属镜头{entry.get('shot_id') or '?'}：{entry.get('shot_range') or '区间未知'}"
        )
    return "\n".join(lines)


LAPIAN_METHOD_GUIDE = """拉片标准：
1. 拉片不是复述剧情，也不是堆“特写/全景/推拉摇移”这些名词；每条结论都必须写出“画面证据 → 幕后选择 → 为什么这样处理 → 观众感受/信息效果 → 可复用方法”。
2. 先用创作者视角看幕后部门：摄影、灯光、美术、道具、演员走位、剪辑、声音分别做了什么，以及它们为什么共同服务这一场戏。
3. 美术/道具不能只写“凌乱/干净/高级”。要追问色块怎么被分割，物件为什么属于这个人物，空间状态如何匹配人物的生存环境、成长经历、阶层关系和当下处境。
4. 分析镜头顺序时重点看信息释放：是否先用特写/具体动作/具体物件抓注意力，再逐层交代地点、身份、关系和处境；如果开场直接用全景，要判断它是否导致信息分散、重点变弱。
5. 每个重点镜头至少给出一个“如果换成普通拍法会少什么”的反向判断，这样才能说明当前选择的必要性。
6. 看不到、听不准、不能从关键帧确认的内容必须写“需人工复核”，禁止把不可见画面、台词、声音硬编出来。
7. 报告必须专业、详细、可交付，不能写成短评、摘要或泛泛建议；只要材料允许，就要具体到镜头顺序、画面证据、部门职责、创作目的和观众效果。
8. 关键镜头拆解必须使用表格或编号条目，每条至少包含：镜头/段落位置、画面证据、景别/机位/构图、幕后部门、创作意图、观众获得的信息/情绪、普通拍法对比、复核点。
9. 视频里出现的字幕、屏幕录制文字、表格、参数、提示词、教程步骤都要当作高价值信息抽取；逐帧 OCR 时宁可标注“看不清”，不要跳过。
10. 音频/字幕要同时服务两件事：完整保留原稿，提炼“讲了什么”；如果字幕里有提示词、模型参数、工具链、镜头描述，要原样摘出并单独归类。
11. 最后必须沉淀成可复用拍法和 AI 短剧提示词写法，写清镜头职责、信息释放顺序、人物站位、道具/色块职责、声音进入点、剪辑节奏、光影、特效和可复刻的画面提示词。
12. 只要有关键帧，就必须优先分析“画面怎么成立”：镜头顺序、硬切/match cut/运动物件接场、主体运动、景别跳跃、画面母题、色彩对撞、标题卡和记忆点；字幕只能辅助判断信息和情绪，不能替代画面分析。
13. 报告必须回答“我怎么做同款”：把原片方法拆成可执行步骤，而不是只写“运镜高级、节奏紧凑、构图好看”。"""


LAPIAN_REPLICATION_SECTIONS = """必须补齐以下可复刻栏目：
【同款复刻总判断】
用 3-5 句话判断这条作品主要靠什么成立：连续运镜、硬切蒙太奇、match cut、运动物件接场、母题重复、AI 合成拼贴、屏录教程、口播说明或其他。必须说明“最值得学什么”和“不要误学什么”。

【镜头/转场复刻表】
必须用表格，列为：段落/帧序｜画面看到什么｜镜头/景别｜机位/运动｜转场方式｜剪辑作用｜可直接照抄的做法。
要求：至少 5 行；每行都要能指导实拍或 AI 视频生成；如果连续运动无法从关键帧确认，写“需回看视频确认”，但仍要给出基于可见画面的复刻建议。

【制作拆解表】
必须用表格，列为：制作环节｜原视频怎么做｜我复制时怎么做｜最低资源方案｜风险点。
制作环节至少覆盖：视觉锚点/主体动作、运镜来源、转场逻辑、景别编排、构图层次、光影色彩、美术道具、表演调度、特效后期、声音字幕。

【最值得学的3个点】
每点都按“原片做法｜为什么有效｜我下次怎么练/怎么照抄”写清楚。

【5镜头极简复刻方案】
用 5 个镜头给出可拍同款骨架。每镜必须写：时长感、画面主体、景别、机位、运动、转场、这一镜承担的信息职责。不要写成剧情摘要。

【导演运镜学习路径】
按“先练什么 → 再练什么 → 最后补什么”输出可执行训练路径，重点讲如何学习原片的镜头组织、转场节奏和画面调度。

【可直接用于AI视频模型的复刻提示词】
输出一段可以给即梦/Seedance/Kling/Sora 使用的中文提示词；必须包含主体、场景、动作调度、景别、机位、运镜、构图、光影、色彩、美术道具、特效/后期、剪辑节奏、镜头时长感和负面约束；不要要求生成水印、平台 UI 或无关字幕。"""


def _vision_analyze(image_paths: list[str], desc: str = "", mode: str = "video") -> str:
    from local_analysis import analyze_images
    return analyze_images(image_paths, desc, mode)


def _timeline_vision_chunk(
    entries: list[dict],
    context: str = "",
    mode: str = "parse",
    prefer_ocr_model: bool = False,
) -> str:
    """把一小组带时间码的证据帧转成逐帧事实记录。

    这一层只做观察，不要求模型一次生成长篇报告，从而避免旧流程因输出过长整组超时。
    """
    global _VISION_BLOCK_REASON
    if not entries:
        return ""
    if _VISION_BLOCK_REASON:
        return f"（{_VISION_BLOCK_REASON}）"
    try:
        from analyzer import get_client, has_api_key
        import local_ai_config as secrets_config
        if not has_api_key():
            return "（本地模型未安装，本组仅保存了带时间码关键帧。）"
        client = get_client()
        try:
            client = client.with_options(timeout=55.0, max_retries=0)
        except Exception:
            pass
        if prefer_ocr_model:
            model = getattr(secrets_config, "OCR_MODEL", "") or getattr(secrets_config, "ANALYZE_MODEL", "")
        else:
            model = getattr(secrets_config, "ANALYZE_MODEL", "") or getattr(secrets_config, "OCR_MODEL", "")
    except Exception as e:
        return f"（时间轴视觉分析未执行：{e}）"

    blocks = []
    media_types = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp"}
    valid_entries = []
    for entry in entries:
        path = entry.get("path") or ""
        if not path or not os.path.exists(path):
            continue
        try:
            ext = path.lower().rsplit(".", 1)[-1]
            with open(path, "rb") as f:
                b64 = base64.standard_b64encode(f.read()).decode()
            label = (
                f"下面图片是帧{int(entry.get('index') or 0):03d}，精确采样时间 "
                f"{entry.get('timecode') or '未知'}，所属硬切镜头区间 "
                f"{entry.get('shot_range') or '未知'}。"
            )
            blocks.append({"type": "text", "text": label})
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media_types.get(ext, "image/jpeg"), "data": b64},
            })
            valid_entries.append(entry)
        except Exception:
            continue
    if not valid_entries:
        return ""

    instruction = f"""你是视频证据记录员。上面的图片严格按时间顺序排列，图片前的文字就是它的真实视频时间码。

当前任务是“{'拉片逐镜取证' if mode == 'lapian' else '视频解析取证'}”，只输出这一组图片的事实观察，不写全片总结，不套通用模板。

必须用 Markdown 表格逐帧输出，列固定为：
帧号/时间码｜可见主体、人物、动作与表情｜景别、机位与构图｜灯光、色彩与氛围｜画面全部可见文字/OCR（提示词、参数、标题、小字都要抄）｜与前一帧相比发生的变化｜连续运镜/声音等不可由静帧确认项。

规则：
1. 每张图片必须单独一行，不得合并、跳帧或把相似帧写成“同上”。
2. OCR 尽量逐字抄；看不清的局部写“□（看不清）”，禁止编字。
3. 景别只能依据可见范围判断；焦段、连续运镜、转场和声音无法由静帧确认时必须明确写“仅凭静帧不可确认”。
4. 可以比较相邻帧的人物姿态、视线、口型、字幕和画面变化，但不能把推测写成事实。
5. 不要输出拍摄建议、五镜头模板、创作公式或空泛评价。

标题/字幕参考仅用于辅助识别，不得覆盖画面事实：
{(context or '')[:2200]}"""
    blocks.append({"type": "text", "text": instruction})

    def _call():
        resp = client.messages.create(
            model=model,
            max_tokens=2600,
            temperature=0.1,
            messages=[{"role": "user", "content": blocks}],
        )
        return "".join(block.text for block in resp.content if block.type == "text").strip()

    q: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            q.put(("ok", _call()))
        except Exception as e:
            q.put(("err", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(60)
    if t.is_alive():
        return "（本组视觉取证超时；对应时间码帧已保存，可单独重试，不影响其他帧组。）"
    try:
        status, value = q.get_nowait()
    except Exception:
        return "（本组视觉取证无返回；对应时间码帧已保存。）"
    if status == "ok":
        return value or "（本帧视觉取证返回空内容。）"
    reason = str(value)
    # 密钥失效/网关拒绝时，剩下的帧必然一起失败；记下原因让后续帧直接跳过，
    # 避免一条视频白等 20 多次超时。
    if re.search(r"authentication|invalid api key|401|permission|APIConnectionError|Connection error", reason, re.I):
        _VISION_BLOCK_REASON = f"视觉模型调用失败，本次跳过其余帧：{reason[:120]}"
    return f"（本帧视觉取证失败：{value}）"


def _timeline_visual_observations(frame_manifest: list[dict], context: str = "", mode: str = "parse", existing_observations: str = "") -> str:
    from local_analysis import timeline_observations
    return timeline_observations(frame_manifest, context, mode, existing_observations)


def _call_lapian_section(instruction: str, max_tokens: int = 4200) -> str:
    """生成一个受控长度的拉片章节，避免整份长报告被模型截断。"""
    try:
        from analyzer import get_client, has_api_key
        import local_ai_config as secrets_config
        if not has_api_key():
            return ""
        client = get_client()
        try:
            client = client.with_options(timeout=100.0, max_retries=0)
        except Exception:
            pass
        # 附录要求长表格且必须完整；中转接口上强视觉/OCR模型的长文本返回也更稳定。
        model = getattr(secrets_config, "OCR_MODEL", "") or getattr(secrets_config, "ANALYZE_MODEL", "")
    except Exception:
        return ""

    q: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=max_tokens,
                temperature=0.1,
                messages=[{"role": "user", "content": instruction}],
            )
            value = "".join(block.text for block in resp.content if block.type == "text").strip()
            q.put(("ok", value))
        except Exception as e:
            q.put(("err", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(105)
    if t.is_alive():
        return ""
    try:
        status, value = q.get_nowait()
    except Exception:
        return ""
    return value if status == "ok" else ""


def _lapian_director_appendix(context: str, observations: str, base_report: str) -> str:
    instruction = f"""你是导演拉片报告编辑。只能依据给定报告和逐帧证据，补齐且只输出以下两个栏目：

【人物、故事与台词全梳理】
写主要人物/主体、造型身份、画内/画外关系、每个真实时间段的行为和情绪、完整故事或教程逻辑。台词只能来自ASR；无ASR就明确写无可确认台词，禁止用画面教学文字伪装对白。

【导演视角完整分析报告】
按导演意图与注意力、镜头组织/信息释放、表演调度、摄影构图、运镜转场、灯光色彩、美术服化道、剪辑节奏、声音台词、开场钩子/段落/收尾分别分析。每项都引用真实时间码并解释为什么有效；不可确认项明确标注。

标题/上下文：{(context or '')[:1800]}

已有报告：{(base_report or '')[:9000]}

逐帧证据：{(observations or '')[:12000]}"""
    return _call_lapian_section(instruction, max_tokens=4400)


def _lapian_replication_appendix(context: str, observations: str, base_report: str) -> str:
    instruction = f"""你是导演拉片报告的复刻表编辑。只能依据给定材料，且只输出一个完整栏目：

【可复刻分镜头提示词表】
必须用 Markdown 表格逐段覆盖全片真实时间轴。列固定为：原片时间段｜生成时长｜首帧画面｜人物/动作调度｜景别/机位/构图｜运镜｜灯光色彩/服化道｜声音/台词/转场｜中文单镜提示词｜负面约束。
硬要求：每个真实段落一行，不套通用五镜头；每个普通单元格不超过45个中文字，单镜提示词不超过150字；不可确认的运镜/声音使用保守方案并标“需回看”。表后不得再写长篇解释。

标题/上下文：{(context or '')[:1600]}

已有报告：{(base_report or '')[:10500]}

逐帧证据：{(observations or '')[:11000]}"""
    return _call_lapian_section(instruction, max_tokens=5000)


def _lapian_closing_appendix(context: str, observations: str, base_report: str) -> str:
    instruction = f"""你是导演拉片报告收尾编辑。只能依据给定材料，且只输出以下两个完整栏目：

【整片复刻总提示词】
给一段可直接用于即梦/Seedance/Kling/Sora 的中文提示词，包含真实总时长、真实镜头结构和各真实时间段、人物造型、动作逻辑、景别构图、光色背景、剪辑/字幕/声音策略、负面约束。无证据的运镜和声音采用保守方案并标需回看；不得生成水印或平台UI。

【仍需人工回看确认】
用 Markdown 表格列：时间段｜待确认项｜证据为何不足｜回看方法｜对复刻的影响。只列真正未确认项。

标题/上下文：{(context or '')[:1600]}

已有报告：{(base_report or '')[:10000]}

逐帧证据：{(observations or '')[:9000]}"""
    return _call_lapian_section(instruction, max_tokens=3400)


def _synthesize_timeline_analysis(frame_manifest: list[dict], observations: str, context: str = "", frame_text: str = "", mode: str = "parse") -> str:
    from local_analysis import timeline_report
    return timeline_report(frame_manifest, observations, context, frame_text, mode)


def _timeline_visual_analyze(
    frame_manifest: list[dict],
    context: str = "",
    frame_text: str = "",
    mode: str = "parse",
    existing_observations: str = "",
) -> tuple[str, str]:
    observations = _timeline_visual_observations(
        frame_manifest,
        context=context,
        mode=mode,
        existing_observations=existing_observations,
    )
    report = _synthesize_timeline_analysis(
        frame_manifest,
        observations,
        context=context,
        frame_text=frame_text,
        mode=mode,
    )
    return report, observations


def _is_weak_ai_result(text: str) -> bool:
    text = (text or "").strip()
    if not text:
        return True
    weak_markers = ("超时", "本地模型未安装", "未执行", "没有返回结果", "视觉分析失败")
    return any(x in text for x in weak_markers)


def _extract_graphic_text_from_images(
    image_paths: list[str],
    desc: str = "",
    max_images: int = 16,
    source_label: str = "关键帧",
) -> str:
    """独立提取视频帧/图文图里的文字、提示词、参数和教程步骤。"""
    usable = [p for p in (image_paths or []) if p and os.path.exists(p)]
    if not usable:
        return ""
    try:
        from analyzer import extract_prompt_from_images
        extracted = extract_prompt_from_images(
            usable,
            desc=(
                "请优先提取视频画面/图文页中的提示词、参数、工具流程、教程步骤、"
                "屏幕文字、画面说明、中文翻译和可复制原文。"
                + ("\n" + desc[:500] if desc else "")
            ),
            max_images=max_images,
        )
    except Exception as e:
        return f"（画面图文信息提取失败：{e}）"

    extracted = (extracted or "").strip()
    if not extracted:
        return f"（已检查 {min(len(usable), max_images)} 张{source_label}，未识别到可整理的提示词、参数、教程步骤或屏幕文字。）"

    return (
        "【图文信息分类汇总】\n"
        "以下内容来自独立本地 RapidOCR，不依赖音频字幕。按原图识别并转为简体，不做模型翻译或补写；"
        "低置信度文字可能漏识，使用前请对照原图。\n\n"
        "【逐帧/逐图完整转录】\n"
        f"{extracted}"
    )


def _call_text_lapian_model(title: str, content: str) -> str:
    try:
        from analyzer import get_client, has_api_key
        import local_ai_config as secrets_config
        if not has_api_key():
            return ""
        client = get_client()
        model = getattr(secrets_config, "ANALYZE_MODEL", "")
    except Exception:
        return ""

    subtitle = _section(content, "字幕") or content
    instruction = f"""你是短剧导演和拉片训练教练。请只根据下面的标题和字幕/文案，生成一份“字幕推导版拉片稿”。

{LAPIAN_METHOD_GUIDE}

即使当前只有字幕/文案，也必须保留下面这些“复刻学习栏目”；凡是不能从字幕确认的画面、运镜、构图、光影、道具和转场，一律写“需关键帧复核”，不要假装看到了。
{LAPIAN_REPLICATION_SECTIONS}

字幕推导版要求：
1. 不要复述剧情，要解释创作者为什么这样安排信息、节奏、动作和情绪。
2. 重点分析信息释放链路：先抛出什么钩子，何时交代人物/地点/关系/冲突，哪里应该用特写、哪里可能需要中景或全景。
3. 看不到画面时必须写“基于字幕推导，需用关键帧复核”，不要编造具体构图、道具、灯光和表演。
4. 结论要能指导下一次创作：哪些地方应该先给具体动作/物件，哪些地方不能一上来全景摊开信息。
5. 字幕里出现的提示词、模型参数、工具名、工作流步骤、镜头描述、英文咒语，要原样摘出，不要翻译、改写或省略。
6. 输出必须专业、详细、可交付；不少于 1200 个中文字。即使只有字幕，也要把“可推导内容”和“待关键帧复核内容”分开写清楚。

输出结构固定：
【专业拉片总评】
【拉片对象与材料状态】
【大概讲什么】
用 3-6 条概括视频核心主题、适用场景、主要干货类型。

【内容分类汇总】
按“提示词/参数｜拍摄技法｜运镜景别｜光影美术｜剪辑节奏｜工具流程｜案例演示｜其他干货”分类；每类标注有无、证据来自哪句字幕、价值高低。

【完整字幕/音频原稿整理】
把字幕按原顺序整理成可读原稿。保留关键原话、模型名、参数、提示词，不要只摘要；如果原字幕已经在下方给出很长，则先分段梳理结构，再标注“完整原文见记录的【字幕】段”。

【提示词/参数/工作流提取】
原文摘出所有可直接复制的提示词、参数、工具链、步骤和镜头描述；没有明确提示词时，写“未从字幕中发现可直接复制的提示词”，再给出基于内容推导的可复刻提示词方向。

【信息释放顺序推导】
【镜头设计推导表】
按“段落/推定镜头｜字幕/文案证据｜建议景别/信息职责｜为什么这样设计｜普通拍法风险｜关键帧复核点”输出 4-8 条。

【同款复刻总判断】
【镜头/转场复刻表】
【制作拆解表】
【最值得学的3个点】
【5镜头极简复刻方案】
【导演运镜学习路径】

【幕后部门复核清单】
分别写摄影/机位/构图、运镜、灯光、美术、道具色块、表演走位、剪辑、声音/字幕、特效/后期，每项都要说明复核重点和创作目的。

【声音与剪辑节奏推导】
【可复用拉片清单】
【可直接用于AI视频模型的复刻提示词】
给一段可直接用于 AI 视频模型的中文复刻提示词，包含主体、场景、动作调度、景别、机位、运镜、构图、光影、色彩、美术道具、特效、剪辑节奏；看不到的视觉信息必须标注“需关键帧复核”，不要生成字幕、水印、平台 UI 或背景音乐。

标题：{title or "未命名视频"}

字幕/文案：
{subtitle[:6000]}"""

    q: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=7000,
                temperature=0.2,
                messages=[{"role": "user", "content": instruction}],
            )
            text = ""
            for block in resp.content:
                if block.type == "text":
                    text += block.text
            q.put(("ok", text.strip()))
        except Exception as e:
            q.put(("err", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(130)
    if t.is_alive():
        return ""
    try:
        status, value = q.get_nowait()
    except Exception:
        return ""
    return value if status == "ok" else ""


def _rule_based_lapian_fallback(title: str, content: str) -> str:
    text = (content or "").replace("\n", "")
    info_points = []
    if any(x in text for x in ("特写", "近景", "动作", "发消息", "哭泣", "健身", "全景")):
        info_points.append("优先检查开场是否用特写/具体动作/具体物件抓住注意力，再逐层交代地点、身份、关系和冲突；如果直接全景开场，要警惕信息过多、重点分散。")
    if any(x in text for x in ("追捕", "紧张", "危机", "逃", "对峙")):
        info_points.append("紧张场景尤其要避免一开始把空间摊平，应该让观众先跟随动作或反应进入压力，再补足空间关系。")
    if not info_points:
        info_points.append("先判断这条视频如何释放信息：开头给钩子，中段补身份/地点/关系，后段完成情绪或方法沉淀。")

    department_points = []
    if any(x in text for x in ("灯光", "美术", "道具", "色块", "凌乱", "场景", "房间", "收纳箱", "画")):
        department_points.append("美术/道具：不要只写“乱”或“整洁”，要看大色块如何被物件分割，道具是否贴合人物身份、成长经历、生活状态和阶层对比。")
    if any(x in text for x in ("固定", "运镜", "横移", "推", "拉", "摇", "快摇", "镜头")):
        department_points.append("摄影/机位：镜头运动必须和戏剧状态绑定，说明它是在稳定关系、强化压迫、保持空间连续，还是制造信息延迟。")
    if any(x in text for x in ("剪辑", "时长", "节奏", "快慢", "段落", "顺序")):
        department_points.append("剪辑：重点不是数镜头，而是判断每次切换释放了什么新信息、制造了什么停顿或推进。")
    if any(x in text for x in ("声音", "音乐", "音效", "BGM", "旁白", "字幕")):
        department_points.append("声音/字幕：判断旁白、音效、字幕与剪辑点是否共同完成提示、悬念、停顿或情绪强化。")
    if any(x in text for x in ("走位", "演员", "表演", "反应", "对峙")):
        department_points.append("表演/走位：看谁先出现、谁先反应、谁占据画面主动位置，位置关系通常是在服务权力和情绪变化。")
    if not department_points:
        department_points.append("摄影、灯光、美术、道具、表演、剪辑、声音都需要逐项复核，不能只停留在内容概述。")

    info_chain = "\n".join(f"{i}. {p}" for i, p in enumerate(info_points, 1))
    department_checklist = "\n".join(f"{i}. {p}" for i, p in enumerate(department_points, 1))
    return f"""【一句话总评】
这条素材应按“画面证据如何指向幕后选择”的方向拉片，重点不是复述发生了什么，而是解释为什么这样拍、这样摆、这样剪。

【拉片对象与材料状态】
当前主要依据标题、字幕/文案和已下载素材生成；具体画面细节需要结合关键帧继续复核。标题：{title or "未命名视频"}。

【大概讲什么】
当前素材围绕标题、字幕或文案提供的信息展开；如果它是教程类视频，需要优先提取提示词、参数、工具链和操作步骤；如果它是案例类视频，需要优先分析成片效果如何由镜头、光影、调度和剪辑共同产生。

【内容分类汇总】
提示词/参数：需检查字幕和画面文字是否出现可复制原文。
拍摄技法：需用关键帧复核景别、机位、焦段、运镜和构图。
光影美术：需用关键帧复核主光方向、色温、明暗对比、道具色块和场景层次。
剪辑节奏：需结合音频、字幕气口和镜头切换点复核。
工具流程：若画面是屏幕录制或教程表格，必须逐帧 OCR 才能确认。

【完整字幕/音频原稿整理】
完整字幕保留在记录正文的【字幕】段。当前兜底报告不会改写原稿；请在复核时从原字幕里逐句标注提示词、参数、工具名、教程步骤和关键表达。

【提示词/参数/工作流提取】
未在规则兜底中确认到可直接复制的提示词。若字幕或画面文字里出现模型名、参数、分镜描述、英文咒语或工作流步骤，应原样摘出，并按“原文｜用途｜适用模型/工具｜需复核点”归类。

【信息释放顺序推导】
{info_chain}

【重点镜头设计推导】
1. 开场钩子镜头｜画面证据：需用关键帧确认是否存在具体动作、手部动作、表情或关键物件｜幕后选择：优先用局部信息吸引注意力｜创作意图：让观众先产生“发生了什么”的追问｜普通拍法风险：直接全景会让地点、人物、动作同时出现，信息重点变弱｜复核点：确认实际景别和动作是否成立。
2. 空间交代镜头｜画面证据：需用关键帧确认场景全貌、人物与环境距离｜幕后选择：在观众被钩住后补充地点和空间关系｜创作意图：让信息从局部扩展到整体｜普通拍法风险：过早交代空间会削弱悬念｜复核点：确认是否有中景/全景或空间转场。
3. 人物关系镜头｜画面证据：需用关键帧确认人物站位、视线方向、前后景关系｜幕后选择：用站位和反应顺序表达谁主动、谁被压迫、谁掌握信息｜创作意图：让关系通过画面自己说话｜普通拍法风险：平铺对话会失去权力变化｜复核点：确认演员走位和剪辑顺序。
4. 美术/道具镜头｜画面证据：需用关键帧确认道具、色块、空间拥挤度或整洁度｜幕后选择：让物件和色块服务人物身份、生活状态或阶层对比｜创作意图：让环境承担叙事信息｜普通拍法风险：只摆常规道具会让人物背景变空｜复核点：确认道具是否具有角色关联。

【同款复刻总判断】
当前为规则兜底版：不能把字幕当成真实运镜证据。先按“视觉锚点 + 景别跳跃 + 硬切/匹配剪辑 + 情绪近景 + 记忆点收尾”的方法搭同款骨架；具体画面、机位、运动和转场必须回看关键帧确认。

【镜头/转场复刻表】
| 段落/帧序 | 画面看到什么 | 镜头/景别 | 机位/运动 | 转场方式 | 剪辑作用 | 可直接照抄的做法 |
|---|---|---|---|---|---|---|
| 开场钩子 | 需确认第 1-2 帧的主体动作、表情或关键物件 | 近景/特写优先 | 固定机位或轻推；也可让主体自己运动 | 直接硬切入 | 先制造追问，不急着交代全貌 | 找一个最有识别度的动作/物件开场，时长 1-2 秒 |
| 主题锚点 | 需确认是否有重复出现的物件、场景或色块 | 中景/物件特写 | 静态构图或慢推 | 同母题硬切 / match cut | 用同一个视觉母题把跳跃画面串起来 | 选择一个“婚纱/门/车/屏幕/道具”等锚点，跨场景反复出现 |
| 推进镜头 | 需确认人物或物件是否在画面中移动 | 中景/跟拍 | 横移、跟拍、主体前进或后退 | 动作接动作 | 让画面看起来有运镜感 | 没有复杂设备时，用人物奔跑、车流、布料、头发、手部动作替代镜头运动 |
| 情绪近景 | 需确认脸部、眼神、手部或局部细节 | 近景/大特写 | 稳定机位，必要时轻推 | 硬切到反应 | 压缩情绪，把观众拉近 | 每 3-5 个画面安排一次表情/手部/道具特写，别一直全景 |
| 环境插镜 | 需确认是否有海、街道、建筑、屏幕、表格等环境信息 | 远景/全景/插入镜头 | 固定或慢摇 | 情绪插镜硬切 | 给节奏换气，补空间与氛围 | 准备 1-2 个不说话的环境镜头，作为段落之间的呼吸点 |
| 高潮记忆点 | 需确认是否有标题卡、强光影、燃烧/闪烁/合成效果 | 全景或强符号镜头 | 固定机位更容易形成海报感 | 硬切到标题/定格 | 让观众记住作品 | 用一个最强的视觉符号收尾，再接标题或口播结论 |

【制作拆解表】
| 制作环节 | 原视频怎么做 | 我复制时怎么做 | 最低资源方案 | 风险点 |
|---|---|---|---|---|
| 视觉锚点/主体动作 | 用一个反复出现的物件、人物状态或场景母题串联段落 | 先选 1 个主母题，再设计 5 个不同场景里的变体 | 一个核心道具 + 两个场景即可 | 锚点不重复，观众会觉得只是素材拼贴 |
| 运镜来源 | 不一定靠复杂设备，常用主体运动、布料/车流/手势制造动感 | 每镜至少安排一个运动来源：人动、物动、光动或镜头动 | 手持轻推 + 人物走动 | 全部静态会变成图集 |
| 转场逻辑 | 多用硬切、同母题匹配、动作方向延续 | 切换前后保持颜色、物件形状、动作方向或人物视线相似 | 同一物件跨场景硬切 | 随机切会失去“高级转场”感觉 |
| 景别编排 | 远景给世界，中景给关系，近景给情绪/细节 | 5 镜里至少包含远景、中景、近景三类 | 手机拍摄也能完成 | 连续同景别会单调 |
| 构图层次 | 前景/主体/背景分层，让画面有深度 | 给画面放前景锚点或背景线条 | 门框、窗、桌面、树影、街灯 | 主体居中站桩容易变海报摆拍 |
| 光影色彩 | 用冷暖、明暗、室内外或昼夜对撞制造情绪变化 | 先定主色，再安排一个反差色作为转场刺激 | 暖灯 + 冷背景或反过来 | 全片同色会疲劳 |
| 美术道具 | 道具不是装饰，要承担人物身份或主题信息 | 每个关键道具都写清“它说明什么” | 1 个主道具 + 2 个辅助道具 | 道具无职责会显廉价 |
| 声音字幕 | 字幕/旁白负责信息，画面负责情绪和证据 | 把字幕只当节奏锚点，不要让报告只复述字幕 | ASR + 人工校对 | 字幕驱动画面会失去拉片价值 |

【最值得学的3个点】
1. 原片做法：先用视觉锚点抓住观众，再补剧情/信息。为什么有效：观众先记住画面，再理解内容。我下次怎么练：先写 5 个画面，不先写剧情梗概。
2. 原片做法：靠硬切、match cut、运动物件让画面“像在运镜”。为什么有效：低成本也能有流动感。我下次怎么练：每个切点都找一个可接的形状、颜色、动作方向或情绪。
3. 原片做法：用近景/特写压情绪，用远景/环境插镜换气。为什么有效：既有冲击也不拥挤。我下次怎么练：每 5 镜至少安排 1 个远景、2 个近景、1 个环境插镜。

【5镜头极简复刻方案】
1. 1-2 秒｜开场钩子｜近景/特写｜固定或轻推｜主体做一个明确动作｜硬切进入｜职责：让观众先问“怎么了”。
2. 2-3 秒｜主题锚点｜中景/物件特写｜静态构图｜展示核心道具/场景母题｜同母题硬切｜职责：建立作品识别物。
3. 2-3 秒｜推进镜头｜中景/跟拍｜横移或主体前进｜人物/车辆/布料制造运动｜动作接动作｜职责：形成高级转场感。
4. 2 秒｜情绪近景｜近景/大特写｜稳定机位轻推｜眼神/手部/表情变化｜硬切到反应｜职责：把情绪压实。
5. 2-3 秒｜记忆点收尾｜远景或强符号镜头｜固定机位｜标题卡/强特效/强构图｜切标题或定格｜职责：给观众留下可传播画面。

【导演运镜学习路径】
先练“每个镜头的信息职责”：开场钩子、空间交代、关系推进、情绪压缩、记忆点。再练“转场连接词”：颜色接颜色、形状接形状、动作方向接动作方向、同一物件跨场景重复。最后练“运动来源”：镜头动、人物动、物件动、光影动四选一，避免全片静态站桩。

【幕后部门复核清单】
{department_checklist}
复核原则：每个部门都要写成“它做了什么、为什么这么做、对观众有什么影响”，不能只列名词。

【声音与剪辑节奏推导】
观察旁白、音乐、音效、字幕和剪辑节拍是否共同制造悬念、紧张、停顿或释放；不要把声音当作附属说明。

【可复用拉片清单】
1. 先问“这段想让观众先知道什么、后知道什么”。
2. 再找“哪个具体动作、物件、站位或色块承担了这个信息”。
3. 然后解释“为什么不用更普通的全景/普通道具/平铺剪法”。
4. 最后沉淀成可复用拍法，而不是停留在剧情复述。
5. 把关键帧复核结果补进镜头级拆解表，形成可直接给导演、摄影、美术、剪辑参考的报告。

【可直接用于AI视频模型的复刻提示词】
基于当前材料可先写成：一个短剧/教程类视频镜头，开场用具体动作或关键物件特写建立钩子，随后用中景交代人物与空间关系，再用反应镜头和道具色块强化信息层级；镜头语言需要写明景别变化、机位高度、运镜目的、构图层次、主光方向、色彩倾向、剪辑节奏、声音进入点和特效痕迹。未被关键帧确认的视觉细节必须保留“需关键帧复核”。"""


def _text_lapian_analyze(title: str, content: str) -> str:
    from local_analysis import text_report
    return text_report(title, content)


def _synthesize_visual_lapian_report(desc: str, grouped_report: str) -> str:
    if not grouped_report:
        return ""
    try:
        from analyzer import get_client, has_api_key
        import local_ai_config as secrets_config
        if not has_api_key():
            return ""
        client = get_client()
        model = getattr(secrets_config, "ANALYZE_MODEL", "")
    except Exception:
        return ""

    instruction = f"""你是短剧导演和拉片报告总编。下面是按关键帧组得到的视觉观察，请把它整合成一份“全片同款复刻报告”。

硬要求：
1. 不要复述剧情，不要写空泛短评；目标是让创作者知道这条片子的运镜、转场、镜头组织和制作方法怎么学。
2. 只能使用下方关键帧组报告和标题/字幕参考中的事实；看不到连续运动、音效或细节时写“需回看视频确认”。
3. 输出必须优先回答“我怎么做同款”，并给出能照着执行的表格。
4. 如果下方分组报告互相重复，请合并去重，形成一张全片级别的制作表。

{LAPIAN_METHOD_GUIDE}

{LAPIAN_REPLICATION_SECTIONS}

标题/字幕参考：
{(desc or "")[:2500]}

关键帧组观察：
{grouped_report[:9000]}"""

    q: queue.Queue = queue.Queue(maxsize=1)

    def _worker():
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=7000,
                temperature=0.15,
                messages=[{"role": "user", "content": instruction}],
            )
            text = ""
            for block in resp.content:
                if block.type == "text":
                    text += block.text
            q.put(("ok", text.strip()))
        except Exception as e:
            q.put(("err", str(e)))

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    t.join(110)
    if t.is_alive():
        return ""
    try:
        status, value = q.get_nowait()
    except Exception:
        return ""
    return value if status == "ok" else ""


def _visual_lapian_analyze(frame_paths: list[str], desc: str) -> str:
    from local_analysis import analyze_images
    return analyze_images(frame_paths, desc, "lapian")


def _compose_lapian_report(title: str, content: str, text_report: str, visual_report: str) -> str:
    if visual_report:
        parts = [
            "【拉片分析】",
            "【关键帧视觉主分析】",
            visual_report.strip(),
            "",
            "【字幕/音频原稿补充】",
            text_report.strip() or _rule_based_lapian_fallback(title, content),
        ]
    else:
        parts = [
            "【拉片分析】",
            "【关键帧视觉主分析】",
            "（未生成关键帧视觉主分析；请下载关键帧人工复核画面细节。下面先给出字幕/文案推导版复刻骨架。）",
            "",
            "【字幕/音频原稿补充】",
            text_report.strip() or _rule_based_lapian_fallback(title, content),
        ]
    parts.extend([
        "",
        "【最终自检】",
        "1. 每条结论都应包含：画面证据、幕后选择、为什么这样处理、观众效果、可复用写法。",
        "2. 先检查信息释放顺序，再讨论单个镜头术语；尤其注意特写/具体动作是否承担开场钩子。",
        "3. 美术和道具分析要落到人物身份、生活状态、色块分割和空间关系，不能只写“乱/高级/有氛围”。",
        "4. 画面中的字幕、屏幕文字、表格、参数、提示词、工具流程必须逐帧 OCR 提取；看不清要标注“需人工复核”。",
        "5. 最终必须保留可复制的提示词/参数原文，并给出一段可复刻原视频效果的 AI 视频提示词。",
        "6. 看不到、听不准、不能确认的内容必须标注“需人工复核”。",
    ])
    return "\n".join(parts).strip()


def _analyze_item(item_id: int, content: str):
    def _work():
        from analyzer import analyze_one, save_analysis, has_api_key
        if not has_api_key():
            return
        result = analyze_one(content, platform="B站")
        conn = get_conn()
        save_analysis(conn, item_id, result)
        conn.close()

    try:
        # AI 分类/提取失败或慢，不影响素材本身落库。
        threading.Thread(target=_work, daemon=True).start()
    except Exception:
        pass


def _build_stat_line(stat: dict) -> str:
    if not stat:
        return ""
    items = []
    mapping = [
        ("view", "播放"), ("like", "点赞"), ("coin", "投币"),
        ("favorite", "收藏"), ("reply", "评论"), ("share", "分享"),
    ]
    for key, label in mapping:
        val = stat.get(key)
        if val not in (None, ""):
            items.append(f"{label}{val}")
    return "，".join(items)


def _process_video(item_id: int, share_text: str, mode: str = "parse"):
    is_lapian = mode == "lapian"
    is_reverse = mode == "reverse_prompt"
    url = _first_url(share_text)
    _progress(item_id, "读取视频信息")
    _log(f"item {item_id}: parse video meta")
    meta = parse_video_meta(url)
    if not meta.get("bvid") or not meta.get("cid"):
        raise RuntimeError("没有解析到 B站视频 bvid/cid")
    key = f"bili_{meta['bvid']}_{meta['cid']}"

    image_paths = []
    _progress(item_id, "下载封面")
    _log(f"item {item_id}: download cover")
    cover_paths = _download_images([meta.get("cover_url")], key, meta["url"], limit=1)
    image_paths.extend(cover_paths)

    _progress(item_id, "下载并合并音视频", meta.get("title") or "")
    _log(f"item {item_id}: download video/audio")
    media = _download_video_audio(meta, key)
    if is_reverse:
        _progress(item_id, "提取首帧并反推提示词")
        frame_paths, frame_manifest = _extract_timeline_frames(
            media.get("video_path") or "",
            key + "_reverse",
            max_frames=1,
            target_interval=999,
        )
        prompt_images = frame_paths[:1] or cover_paths[:1]
        try:
            from reference_prompt import generate_dual_prompt_report

            reverse_prompt = generate_dual_prompt_report(
                prompt_images,
                context=(meta.get("title") or "") + "\n" + (meta.get("desc") or ""),
                source_type="video_first_frame",
                labels=["视频首帧" if frame_paths else "视频封面（首帧提取失败时的替代素材）"],
                max_images=1,
            ) if prompt_images else "（未能提取视频首帧或封面，无法反推提示词。）"
        except Exception as e:
            reverse_prompt = f"（视频首帧提示词反推失败：{e}）"
        parts = [f"【标题】{meta.get('title') or 'B站视频'}"]
        if meta.get("author"):
            parts.append(f"【作者】{meta['author']}")
        if frame_manifest:
            parts.append("【首帧信息】\n" + _timeline_manifest_text(frame_manifest, max_items=1))
        parts.append(f"【文生图/首帧图反推提示词】\n{reverse_prompt}")
        images = prompt_images
        raw = {
            "kind": "video",
            "bvid": meta.get("bvid"),
            "cid": meta.get("cid"),
            "share_url": meta.get("url"),
            "video_path": media.get("video_path", ""),
            "audio_path": media.get("audio_path", ""),
            "cover_path": cover_paths[0] if cover_paths else "",
            "frame_paths": frame_paths,
            "frame_manifest": frame_manifest,
            "image_paths": images,
            "image_urls": [_as_local_url(p) for p in images],
            "reverse_prompt_analysis": reverse_prompt,
            "analysis_mode": "reverse_prompt",
        }
        _update_item(
            item_id,
            platform_id=f"bili_{meta['bvid']}_{meta['cid']}_{item_id}",
            content_type="提示词反推",
            title=meta.get("title") or "B站视频",
            author_name=meta.get("author") or "",
            author_id=meta.get("author_id") or "",
            publish_time=meta.get("publish_time") or "",
            content="\n\n".join(parts),
            media_urls=" ".join(raw["image_urls"]),
            source_url=meta.get("url") or url,
            raw_json=json.dumps(raw, ensure_ascii=False),
            status="完成",
        )
        _log(f"item {item_id}: reverse prompt done")
        return

    _progress(item_id, "提取字幕", "优先读取 B站官方字幕；没有官方字幕时会使用本地 ASR。")
    _log(f"item {item_id}: subtitle")
    subtitle, sub_source = _fetch_official_subtitle(meta)
    asr_audio = ""
    if not subtitle:
        subtitle, asr_audio, sub_source = _asr_subtitle(media.get("video_path") or "")
    if asr_audio and os.path.exists(asr_audio):
        media["audio_path"] = asr_audio

    _progress(item_id, "抽取关键帧")
    _log(f"item {item_id}: extract frames")
    frame_paths = _extract_frames(media.get("video_path") or "", key, max_frames=16 if is_lapian else 12)
    image_paths.extend(frame_paths)
    _progress(item_id, "提取视频画面图文", "自动整理关键帧中的提示词、参数、教程步骤、屏幕文字和图文干货。")
    _log(f"item {item_id}: frame text ocr")
    frame_text = _extract_graphic_text_from_images(
        frame_paths or cover_paths,
        desc=(meta.get("title") or "") + "\n" + (meta.get("desc") or ""),
        max_images=16 if is_lapian else 12,
        source_label="关键帧",
    )
    _progress(
        item_id,
        "生成拉片分析" if is_lapian else "分析画面内容",
        "本地 CPU 逐帧分析约需数分钟；已完成的帧会缓存，超时会保留素材和真实识别结果。",
    )
    _log(f"item {item_id}: vision analyze")
    vision_desc = (meta.get("title") or "") + "\n" + (meta.get("desc") or "")
    if subtitle:
        vision_desc += "\n\n字幕参考：\n" + subtitle[:2500]
    if is_lapian:
        text_source = "\n\n".join([
            f"【标题】{meta.get('title') or 'B站视频'}",
            f"【简介】\n{meta.get('desc') or ''}",
            f"【视频图文信息提取】\n{frame_text or '（未提取到视频画面图文信息）'}",
            f"【字幕】\n{subtitle or '（未提取到字幕）'}",
        ])
        text_report = _text_lapian_analyze(meta.get("title") or "B站视频", text_source)
        visual_report = _visual_lapian_analyze(frame_paths or cover_paths, desc=vision_desc)
        visual = _compose_lapian_report(meta.get("title") or "B站视频", text_source, text_report, visual_report)
    else:
        visual = _vision_analyze(frame_paths or cover_paths, desc=vision_desc, mode="video")
    _log(f"item {item_id}: write db")

    stat_line = _build_stat_line(meta.get("stat") or {})
    parts = [f"【标题】{meta.get('title') or 'B站视频'}"]
    if meta.get("author"):
        parts.append(f"【作者】{meta['author']}")
    if meta.get("publish_time"):
        parts.append(f"【发布时间】{meta['publish_time']}")
    info = [f"BVID：{meta.get('bvid')}", f"CID：{meta.get('cid')}"]
    if meta.get("duration"):
        info.append(f"时长：{meta['duration']}秒")
    if stat_line:
        info.append(f"数据：{stat_line}")
    if meta.get("tags"):
        info.append("标签：" + "、".join(meta["tags"]))
    parts.append("【视频信息】\n" + "\n".join(x for x in info if x))
    if meta.get("desc"):
        parts.append(f"【简介】\n{meta['desc']}")
    if subtitle:
        parts.append(f"【字幕】（来源：{sub_source}）\n{subtitle}")
    else:
        parts.append("【字幕】\n（未提取到字幕）")
    if frame_text:
        parts.append(f"【视频图文信息提取】\n{frame_text}")
    if visual:
        parts.append(visual if is_lapian else f"【画面内容分析】\n{visual}")
    content = "\n\n".join(parts)
    raw = {
        "kind": "video",
        "bvid": meta.get("bvid"),
        "cid": meta.get("cid"),
        "share_url": meta.get("url"),
        "video_path": media.get("video_path", ""),
        "audio_path": media.get("audio_path", ""),
        "video_stream_path": media.get("video_stream_path", ""),
        "audio_stream_path": media.get("audio_stream_path", ""),
        "cover_path": cover_paths[0] if cover_paths else "",
        "frame_paths": frame_paths,
        "image_paths": image_paths,
        "image_urls": [_as_local_url(p) for p in image_paths],
        "subtitle_source": sub_source,
        "frame_text_extraction": frame_text,
        "visual_analysis": visual,
        "analysis_mode": mode,
        "stat": meta.get("stat") or {},
    }
    _update_item(
        item_id,
        platform_id=f"bili_{meta['bvid']}_{meta['cid']}_{item_id}",
        content_type="拉片" if is_lapian else "视频",
        title=meta.get("title") or "B站视频",
        author_name=meta.get("author") or "",
        author_id=meta.get("author_id") or "",
        publish_time=meta.get("publish_time") or "",
        content=content,
        media_urls=" ".join(raw["image_urls"]),
        like_count=int((meta.get("stat") or {}).get("like") or 0),
        comment_count=int((meta.get("stat") or {}).get("reply") or 0),
        share_count=int((meta.get("stat") or {}).get("share") or 0),
        source_url=meta.get("url") or url,
        raw_json=json.dumps(raw, ensure_ascii=False),
        status="完成",
    )
    _log(f"item {item_id}: done")
    _analyze_item(item_id, f'【标题】{meta.get("title", "")}\n{subtitle}\n{frame_text}')


def _process_image_text(item_id: int, share_text: str, mode: str = "parse"):
    url = _first_url(share_text)
    _progress(item_id, "读取图文信息")
    _log(f"item {item_id}: parse image text")
    data = parse_image_text(url)
    key = "bili_img_" + _sha1(data.get("url") or url)
    _progress(item_id, "下载图文图片")
    image_paths = _download_images(data.get("image_urls") or [], key, data.get("url") or url, limit=20)
    if mode == "reverse_prompt":
        try:
            from reference_prompt import generate_dual_prompt_report

            reverse_prompt = generate_dual_prompt_report(
                image_paths,
                context=(data.get("title") or "") + "\n" + (data.get("content") or ""),
                source_type="image",
                labels=[f"B站图文参考图{i}" for i in range(1, len(image_paths) + 1)],
                max_images=8,
            ) if image_paths else "（没有下载到可用图片，无法反推提示词。）"
        except Exception as e:
            reverse_prompt = f"（图文参考图提示词反推失败：{e}）"
        parts = [f"【标题】{data.get('title') or 'B站图文'}"]
        if data.get("author"):
            parts.append(f"【作者】{data['author']}")
        parts.append(f"【文生图/首帧图反推提示词】\n{reverse_prompt}")
        raw = {
            "kind": "image_text",
            "share_url": data.get("url") or url,
            "image_paths": image_paths,
            "image_urls": [_as_local_url(p) for p in image_paths],
            "reverse_prompt_analysis": reverse_prompt,
            "analysis_mode": "reverse_prompt",
        }
        _update_item(
            item_id,
            platform_id=f"bili_img_{_sha1(data.get('url') or url)}_{item_id}",
            content_type="提示词反推",
            title=data.get("title") or "B站图文",
            author_name=data.get("author") or "",
            publish_time=data.get("publish_time") or "",
            content="\n\n".join(parts),
            media_urls=" ".join(raw["image_urls"]),
            source_url=data.get("url") or url,
            raw_json=json.dumps(raw, ensure_ascii=False),
            status="完成",
        )
        return

    _progress(item_id, "分析图文内容")
    visual = _vision_analyze(image_paths, desc=(data.get("title") or "") + "\n" + (data.get("content") or ""), mode="image")

    parts = [f"【标题】{data.get('title') or 'B站图文'}"]
    if data.get("author"):
        parts.append(f"【作者】{data['author']}")
    if data.get("content"):
        parts.append(f"【正文】\n{data['content']}")
    elif data.get("desc"):
        parts.append(f"【正文】\n{data['desc']}")
    if visual:
        parts.append(f"【图文内容分析】\n{visual}")
    content = "\n\n".join(parts)
    raw = {
        "kind": "image_text",
        "share_url": data.get("url") or url,
        "image_paths": image_paths,
        "image_urls": [_as_local_url(p) for p in image_paths],
        "visual_analysis": visual,
        "analysis_mode": mode,
    }
    _update_item(
        item_id,
        platform_id=f"bili_img_{_sha1(data.get('url') or url)}_{item_id}",
        content_type="图文",
        title=data.get("title") or "B站图文",
        author_name=data.get("author") or "",
        publish_time=data.get("publish_time") or "",
        content=content,
        media_urls=" ".join(raw["image_urls"]),
        source_url=data.get("url") or url,
        raw_json=json.dumps(raw, ensure_ascii=False),
        status="完成",
    )
    _analyze_item(item_id, content)


def _do_process(item_id: int, share_text: str, mode: str = "parse"):
    try:
        _log(f"item {item_id}: start")
        url = _first_url(share_text)
        if "/video/" in url or re.search(r"BV[0-9A-Za-z]+", url):
            _process_video(item_id, share_text, mode=mode)
        else:
            _process_image_text(item_id, share_text, mode=mode)
    except Exception as e:
        _log(f"item {item_id}: failed {e}")
        _update_item(
            item_id,
            title="B站解析失败",
            content=f"处理出错：{e}",
            raw_json=json.dumps({"share_text": share_text, "error": str(e), "kind": "failed"}, ensure_ascii=False),
            status="失败",
        )


def process_bilibili_link(share_text: str, mode: str = "parse") -> dict:
    if not share_text:
        return {"ok": False, "error": "请粘贴 B站链接"}
    if mode not in ("parse", "lapian", "reverse_prompt"):
        mode = "parse"
    url = _first_url(share_text)
    bvid_match = re.search(r"(BV[0-9A-Za-z]+)", url)
    if bvid_match and mode == "parse":
        bvid = bvid_match.group(1)
        try:
            conn = get_conn()
            row = conn.execute(
                "SELECT id FROM items WHERE platform='B站' AND platform_id LIKE ? AND status='完成' "
                "ORDER BY id DESC LIMIT 1",
                (f"bili_{bvid}_%",),
            ).fetchone()
            conn.close()
            if row:
                return {"ok": True, "cached": True, "item_id": row[0], "platform": "B站", "mode": mode}
        except Exception:
            pass
    item_id = create_placeholder(share_text, mode=mode)
    if not item_id:
        return {"ok": False, "error": "创建 B站解析任务失败"}
    t = threading.Thread(target=_do_process, args=(item_id, share_text, mode), daemon=True)
    t.start()
    return {"ok": True, "async": True, "item_id": item_id, "platform": "B站", "mode": mode}


def reset_stuck_processing(timeout_minutes: int = 90) -> int:
    """Mark old Bilibili processing rows as failed after server restarts or lost threads."""
    try:
        cutoff = (datetime.datetime.now() - datetime.timedelta(minutes=timeout_minutes)).isoformat()
        conn = get_conn()
        cur = conn.execute(
            "UPDATE items SET status='失败', title='B站解析超时', "
            "content='解析任务已超时或服务重启导致后台线程丢失，请重新提交链接。' "
            "WHERE platform='B站' AND status='处理中' AND crawled_at < ?",
            (cutoff,),
        )
        n = cur.rowcount
        conn.commit()
        conn.close()
        return n
    except Exception:
        return 0


def _read_item(item_id: str) -> dict | None:
    conn = get_conn()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        "SELECT title, content, raw_json, platform, content_type FROM items WHERE id=? AND platform='B站'",
        (item_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _download_response(item, kind: str):
    title = _safe_name(item.get("title") or "bilibili")
    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except Exception:
        raw = {}

    if kind == "text":
        buf = io.BytesIO(("﻿" + (item.get("content") or "")).encode("utf-8"))
        return send_file(buf, mimetype="text/plain; charset=utf-8", as_attachment=True, download_name=f"{title}.txt")

    if kind in ("images", "frames"):
        key = "frame_paths" if kind == "frames" else "image_paths"
        paths = raw.get(key) or []
        if kind == "images" and not paths:
            paths = raw.get("frame_paths") or []
        paths = [p for p in paths if p and os.path.exists(p)]
        if not paths:
            return "没有可下载的图片/关键帧", 404
        mem = io.BytesIO()
        with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
            for i, p in enumerate(paths, 1):
                ext = os.path.splitext(p)[1] or ".jpg"
                zf.write(p, arcname=f"{i:02d}{ext}")
        mem.seek(0)
        suffix = "frames" if kind == "frames" else "images"
        return send_file(mem, mimetype="application/zip", as_attachment=True, download_name=f"{title}_{suffix}.zip")

    if kind == "report":
        return _report_response(item, raw)

    path_key = "video_path" if kind == "video" else "audio_path"
    path = raw.get(path_key) or ""
    if not path or not os.path.exists(path):
        return "文件不存在（可能该内容不是视频，或解析失败）", 404
    ext = os.path.splitext(path)[1] or (".mp4" if kind == "video" else ".m4a")
    return send_file(path, as_attachment=True, download_name=f"{title}{ext}")


def _add_structured_text_to_doc(doc, text: str):
    for para in (text or "").split("\n"):
        line = para.strip()
        if re.fullmatch(r"【[^】]{2,30}】", line):
            doc.add_heading(line.strip("【】"), level=2)
        elif line:
            doc.add_paragraph(line)
        else:
            doc.add_paragraph("")


def _report_response(item: dict, raw: dict):
    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    platform = item.get("platform") or "素材"
    title = item.get("title") or f"{platform}素材报告"
    is_lapian = raw.get("analysis_mode") == "lapian" or item.get("content_type") == "拉片"
    is_reverse = raw.get("analysis_mode") == "reverse_prompt" or item.get("content_type") == "提示词反推"
    report_title = f"{platform}拉片报告" if is_lapian else (f"{platform}提示词反推报告" if is_reverse else f"{platform}素材解析报告")
    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph(report_title, style="Subtitle")
    meta = [
        ("平台", platform),
        ("类型", item.get("content_type") or raw.get("kind") or ""),
        ("BVID", raw.get("bvid") or ""),
        ("CID", str(raw.get("cid") or "")),
        ("原链接", raw.get("share_url") or ""),
        ("字幕来源", raw.get("subtitle_source") or ""),
    ]
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for k, v in meta:
        if not v:
            continue
        cells = tbl.add_row().cells
        cells[0].text = k
        cells[1].text = str(v)

    content = item.get("content") or ""
    graphic = (
        _section(content, "视频图文信息提取")
        or _section(content, "图片图文信息提取")
        or _section(content, "图片提示词")
    )
    if graphic:
        doc.add_heading("图文信息提取", level=1)
        _add_structured_text_to_doc(doc, graphic)

    lapian = _section(content, "拉片分析")
    if lapian:
        doc.add_heading("拉片分析", level=1)
        _add_structured_text_to_doc(doc, lapian)

    doc.add_heading("完整梳理内容", level=1)
    for para in content.split("\n"):
        doc.add_paragraph(para)

    paths = raw.get("image_paths") or raw.get("frame_paths") or []
    paths = [p for p in paths if p and os.path.exists(p)]
    if paths:
        doc.add_heading("图片/关键帧", level=1)
        for p in paths[:20]:
            try:
                doc.add_picture(p, width=Inches(4.8))
            except Exception:
                pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = _safe_name(title)
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{safe}_{report_title}.docx",
    )


def _read_any_item(item_id: str) -> dict | None:
    conn = get_conn()
    conn.row_factory = __import__("sqlite3").Row
    row = conn.execute(
        """SELECT i.id, i.title, i.author_name, i.publish_time, i.content,
                  i.source_url, i.crawled_at, i.raw_json, i.platform, i.content_type,
                  i.status,
                  a.is_prompt, a.prompt_text, a.prompt_lang, a.category,
                  a.quality_score, a.summary_zh, a.reason
           FROM items i LEFT JOIN analysis a ON a.item_id=i.id
           WHERE i.id=?""",
        (item_id,),
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def _mark_link_source(item_id: int, mode: str = "parse"):
    try:
        item = _read_any_item(str(item_id))
        raw = {}
        if item:
            try:
                raw = json.loads(item.get("raw_json") or "{}")
            except Exception:
                raw = {}
        raw["analysis_mode"] = mode
        fields = {
            "matched_keyword": LINK_SOURCE_TAG,
            "raw_json": json.dumps(raw, ensure_ascii=False),
        }
        if mode == "lapian":
            fields["content_type"] = "拉片"
        elif mode == "reverse_prompt":
            fields["content_type"] = "提示词反推"
        _update_item(item_id, **fields)
    except Exception:
        pass


def _section(content: str, name: str) -> str:
    marker = f"【{name}】"
    if marker not in (content or ""):
        return ""
    tail = content.split(marker, 1)[1].strip()
    if name == "拉片分析":
        return tail
    next_m = re.search(r"\n\n【[^】]+】", tail)
    return tail[:next_m.start()].strip() if next_m else tail.strip()


def finalize_bilibili_lapian(item_id: int, force: bool = False) -> dict:
    item = _read_any_item(str(item_id))
    if not item:
        return {"ok": False, "error": "记录不存在"}
    if item.get("status") not in ("完成", "处理中"):
        return {"ok": False, "error": f"记录尚未完成：{item.get('status') or ''}"}
    if item.get("platform") != "B站":
        return {"ok": False, "error": "只支持 B站记录补拉片"}

    title = item.get("title") or "B站视频"
    content = item.get("content") or ""
    if not force and item.get("content_type") == "拉片" and "【拉片分析】" in content:
        return {"ok": True, "item_id": item_id, "skipped": True, "msg": "已有拉片分析"}

    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except Exception:
        raw = {}
    raw["analysis_mode"] = "lapian"

    text_report = _text_lapian_analyze(title, content)
    video_path = raw.get("video_path") or ""
    cover_path = raw.get("cover_path") or ""
    frame_paths = []
    visual_report = ""

    if video_path and os.path.exists(video_path):
        key = f"bili_lapian_{item_id}_{_sha1(video_path)}"
        frame_paths = _extract_frames(video_path, key, max_frames=16)
        visual_source = frame_paths
        if not visual_source and cover_path and os.path.exists(cover_path):
            visual_source = [cover_path]
        desc = f"{title}\n\n{content[:3500]}"
        visual_report = _visual_lapian_analyze(visual_source, desc=desc)
    else:
        frame_paths = [p for p in (raw.get("frame_paths") or []) if p and os.path.exists(p)]
        if frame_paths:
            desc = f"{title}\n\n{content[:3500]}"
            visual_report = _visual_lapian_analyze(frame_paths, desc=desc)
        else:
            visual_report = "（该记录没有可用视频文件或关键帧；当前为字幕/文案推导版拉片。）"

    image_paths = []
    if cover_path and os.path.exists(cover_path):
        image_paths.append(cover_path)
    image_paths.extend(frame_paths)
    raw["frame_paths"] = frame_paths
    raw["image_paths"] = image_paths or frame_paths
    raw["image_urls"] = [_as_local_url(p) for p in (raw.get("image_paths") or [])]
    raw["visual_analysis"] = _compose_lapian_report(title, content, text_report, visual_report)

    base = content.split("【拉片分析】", 1)[0].rstrip()
    new_content = base + "\n\n" + raw["visual_analysis"]
    _update_item(
        item_id,
        matched_keyword=LINK_SOURCE_TAG,
        content_type="拉片",
        content=new_content,
        media_urls=" ".join(raw.get("image_urls") or []),
        raw_json=json.dumps(raw, ensure_ascii=False),
        status="完成",
    )
    return {
        "ok": True,
        "item_id": item_id,
        "frames": len(frame_paths or []),
        "visual_weak": _is_weak_ai_result(visual_report),
    }


def _douyin_material_report_response(item_id: str):
    item = _read_any_item(item_id)
    if not item or item.get("platform") != "抖音":
        return "找不到该抖音记录", 404
    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except Exception:
        raw = {}

    if raw.get("analysis_mode") == "parse" and raw.get("kind") == "video":
        from video_parse_report import build_video_parse_report

        buf = build_video_parse_report(item, raw)
        safe = _safe_name(item.get("title") or "抖音视频")
        return send_file(
            buf,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            as_attachment=True,
            download_name=f"{safe}_抖音视频解析报告.docx",
        )

    from docx import Document
    from docx.shared import Inches, Pt, RGBColor

    title = item.get("title") or "抖音素材报告"
    doc = Document()
    doc.add_heading(title, level=0)
    doc.add_paragraph("抖音素材解析报告", style="Subtitle")

    meta = [
        ("平台", "抖音"),
        ("类型", item.get("content_type") or raw.get("kind") or ""),
        ("作者", item.get("author_name") or ""),
        ("发布时间", item.get("publish_time") or ""),
        ("解析时间", item.get("crawled_at") or ""),
        ("原链接", item.get("source_url") or raw.get("share_url") or ""),
        ("模式", "拉片" if raw.get("analysis_mode") == "lapian" else "解析"),
    ]
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for k, v in meta:
        if not v:
            continue
        cells = tbl.add_row().cells
        cells[0].text = k
        cells[1].text = str(v)

    if item.get("summary_zh"):
        doc.add_heading("AI 摘要", level=1)
        doc.add_paragraph(item["summary_zh"])
    if item.get("reason"):
        doc.add_heading("AI 判定理由", level=1)
        doc.add_paragraph(item["reason"])

    prompt = (item.get("prompt_text") or "").strip()
    if prompt:
        doc.add_heading("提取出的提示词", level=1)
        p = doc.add_paragraph()
        run = p.add_run(prompt)
        run.font.name = "Consolas"
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x0B, 0x6B, 0x2E)

    content = to_simplified(item.get("content") or "")
    lapian = _section(content, "拉片分析")
    if lapian:
        doc.add_heading("拉片分析", level=1)
        _add_structured_text_to_doc(doc, lapian)

    doc.add_heading("完整梳理内容", level=1)
    for para in content.split("\n"):
        doc.add_paragraph(para)

    paths = raw.get("image_paths") or raw.get("frame_paths") or []
    paths = [p for p in paths if p and os.path.exists(p)]
    if paths:
        manifest_by_path = {
            os.path.normcase(os.path.abspath(str(entry.get("path") or ""))): entry
            for entry in (raw.get("frame_manifest") or [])
            if entry.get("path")
        }
        doc.add_heading(f"关键帧时间轴（{len(paths)} 张）", level=1)
        for p in paths[:60]:
            try:
                entry = manifest_by_path.get(os.path.normcase(os.path.abspath(p))) or {}
                if entry:
                    doc.add_paragraph(
                        f"帧{int(entry.get('index') or 0):03d}｜{entry.get('timecode') or '时间码未知'}｜"
                        f"所属镜头{entry.get('shot_id') or '?'}：{entry.get('shot_range') or '区间未知'}"
                    )
                doc.add_picture(p, width=Inches(4.8))
            except Exception:
                pass

    buf = io.BytesIO()
    doc.save(buf)
    buf.seek(0)
    safe = _safe_name(title)
    suffix = "抖音拉片报告" if raw.get("analysis_mode") == "lapian" else "抖音素材解析报告"
    return send_file(
        buf,
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        as_attachment=True,
        download_name=f"{safe}_{suffix}.docx",
    )


def finalize_douyin_lapian(item_id: int, force: bool = False) -> dict:
    item = _read_any_item(str(item_id))
    if not item:
        return {"ok": False, "error": "记录不存在"}
    if item.get("status") not in ("完成", "处理中"):
        return {"ok": False, "error": f"记录尚未完成：{item.get('status') or ''}"}
    if item.get("platform") != "抖音":
        return {"ok": False, "error": "只支持抖音记录补拉片"}

    title = item.get("title") or "抖音视频"
    content = item.get("content") or ""
    if not force and item.get("content_type") == "拉片" and "【拉片分析】" in content:
        return {"ok": True, "item_id": item_id, "skipped": True, "msg": "已有拉片分析"}

    try:
        raw = json.loads(item.get("raw_json") or "{}")
    except Exception:
        raw = {}
    raw["analysis_mode"] = "lapian"

    video_path = raw.get("video_path") or ""
    frame_paths = raw.get("frame_paths") or []
    frame_manifest = raw.get("frame_manifest") or []
    frame_text = raw.get("frame_text_extraction") or ""
    visual_report = ""
    observations = ""
    existing_observations = raw.get("visual_observations") or ""
    raw.pop("reverse_prompt_analysis", None)

    if video_path and os.path.exists(video_path):
        key = f"douyin_lapian_{item_id}_{_sha1(video_path)}"
        if force or not frame_paths or not frame_manifest:
            frame_paths, frame_manifest = _extract_timeline_frames(
                video_path,
                key,
                max_frames=36,
                target_interval=0.85,
            )
        raw["frame_paths"] = frame_paths
        raw["frame_manifest"] = frame_manifest
        raw["image_paths"] = frame_paths
        raw["image_urls"] = [_as_local_url(p) for p in (raw.get("image_paths") or [])]
        # 去掉旧的超时/模板分析，只把标题、作者、字幕等原始材料交给新时间轴流程。
        cut_positions = [
            pos for marker in ("【视频图文信息提取】", "【画面内容分析】", "【拉片分析】", "【文生图/首帧图反推提示词】")
            if (pos := content.find(marker)) >= 0
        ]
        source_base = content[:min(cut_positions)].rstrip() if cut_positions else content.rstrip()
        desc = f"{title}\n\n{source_base[:8500]}"
        frame_text = "【帧号与时间码对照】\n" + _timeline_manifest_text(frame_manifest, max_items=60)
        visual_report, observations = _timeline_visual_analyze(
            frame_manifest,
            context=desc,
            frame_text=frame_text,
            mode="lapian",
            existing_observations=existing_observations,
        )
        if observations:
            frame_text += "\n\n【逐帧画面文字/OCR证据】\n" + observations
        raw["frame_text_extraction"] = frame_text
        raw["visual_observations"] = observations
    else:
        raw["image_urls"] = raw.get("image_urls") or [_as_local_url(p) for p in (raw.get("image_paths") or [])]
        visual_report = "（该记录没有可下载视频文件，无法生成逐帧证据和拉片表；程序未使用通用模板冒充真实分析。）"

    cut_positions = [
        pos for marker in ("【视频图文信息提取】", "【画面内容分析】", "【拉片分析】", "【文生图/首帧图反推提示词】")
        if (pos := content.find(marker)) >= 0
    ]
    base = content[:min(cut_positions)].rstrip() if cut_positions else content.rstrip()
    if frame_text:
        base += "\n\n【视频图文信息提取】\n" + frame_text
    report = "【拉片分析】\n" + visual_report.strip()
    raw["visual_analysis"] = report
    new_content = base + "\n\n" + report
    _update_item(
        item_id,
        matched_keyword=LINK_SOURCE_TAG,
        content_type="拉片",
        content=new_content,
        media_urls=" ".join(raw.get("image_urls") or []),
        raw_json=json.dumps(raw, ensure_ascii=False),
        status="完成",
    )
    return {
        "ok": True,
        "item_id": item_id,
        "frames": len(frame_paths or []),
        "visual_weak": _is_weak_ai_result(visual_report),
    }


def _schedule_douyin_lapian(item_id: int):
    def _work():
        deadline = time.time() + 1800
        item = None
        while time.time() < deadline:
            item = _read_any_item(str(item_id))
            if not item:
                return
            if item.get("status") in ("完成", "失败"):
                break
            time.sleep(4)
        if not item or item.get("status") != "完成":
            return
        finalize_douyin_lapian(item_id, force=True)

    threading.Thread(target=_work, daemon=True).start()


def _schedule_existing_lapian(item_id: int, force: bool = True) -> dict:
    item = _read_any_item(str(item_id))
    if not item:
        return {"ok": False, "error": "记录不存在"}
    if item.get("status") != "完成":
        return {"ok": False, "error": f"记录尚未完成：{item.get('status') or ''}"}
    platform = item.get("platform") or ""
    if platform not in ("B站", "抖音"):
        return {"ok": False, "error": "当前只支持 B站和抖音视频补拉片"}

    original_content = item.get("content") or ""
    try:
        _update_item(item_id, status="处理中")
    except Exception:
        pass

    def _work():
        try:
            if platform == "B站":
                result = finalize_bilibili_lapian(item_id, force=force)
            else:
                result = finalize_douyin_lapian(item_id, force=force)
            if result.get("ok"):
                if result.get("skipped"):
                    _update_item(item_id, status="完成")
                return
            _update_item(
                item_id,
                status="完成",
                content=(original_content.rstrip() + f"\n\n【拉片失败】\n{result.get('error') or '未知错误'}").strip(),
            )
        except Exception as e:
            _update_item(
                item_id,
                status="完成",
                content=(original_content.rstrip() + f"\n\n【拉片失败】\n{e}").strip(),
            )

    threading.Thread(target=_work, daemon=True).start()
    return {"ok": True, "async": True, "item_id": item_id, "platform": platform, "mode": "lapian"}


def register_routes(server_module):
    app = server_module.app
    original_extract = app.view_functions.get("api_extract")
    original_list = app.view_functions.get("api_extract_list")
    original_douyin_download = app.view_functions.get("douyin_download")
    original_extract_delete = app.view_functions.get("api_extract_delete")
    original_item_delete = app.view_functions.get("api_item_delete")

    def home_redirect():
        return redirect("/")

    def api_extract_ext():
        body = request.json or {}
        url = (body.get("url") or body.get("share_text") or "").strip()
        mode = (body.get("mode") or "parse").strip()
        if mode not in ("parse", "lapian", "reverse_prompt"):
            mode = "parse"
        if is_bilibili_url(url):
            result = process_bilibili_link(url, mode=mode)
            try:
                from auth_manager import link_item_to_user
                if result.get("item_id"):
                    link_item_to_user(result.get("item_id"), action_mode=mode)
            except Exception:
                pass
            return jsonify(result)
        if is_douyin_url(url):
            try:
                from crawler_douyin import process_douyin_link
                from auth_manager import link_item_to_user
                result = process_douyin_link(url, do_subtitle=True, mode=mode)
                item_id = result.get("item_id")
                if item_id:
                    _mark_link_source(item_id, mode=mode)
                    link_item_to_user(item_id, action_mode=mode)
                result["async"] = True
                result["platform"] = "抖音"
                result["mode"] = mode
                return jsonify(result)
            except Exception as e:
                return jsonify({"ok": False, "error": f"抖音解析失败: {e}"}), 500
        if mode == "reverse_prompt":
            return jsonify({
                "ok": False,
                "error": "链接反推目前支持抖音和 B站视频；图片请清空输入框后点击「反推」上传。",
            }), 400
        result = original_extract()
        try:
            payload = result.get_json(silent=True)
            from auth_manager import link_item_to_user
            if payload and payload.get("item_id"):
                link_item_to_user(payload.get("item_id"), action_mode=mode)
        except Exception:
            pass
        return result

    def api_extract_list_ext():
        from auth_manager import _records_query, serialize_record
        out = [
            serialize_record(r)
            for r in _records_query(
                q=(request.args.get("q") or "").strip(),
                platform=(request.args.get("platform") or "").strip(),
                content_type=(request.args.get("content_type") or "").strip(),
                category=(request.args.get("category") or "").strip(),
                limit=int(request.args.get("limit") or 300),
            )
        ]
        return jsonify({"items": out, "total": len(out)})

    def api_extract_lapian():
        body = request.json or {}
        item_id = str(body.get("id") or body.get("item_id") or "").strip()
        if not item_id:
            return jsonify({"ok": False, "error": "缺少 item_id"}), 400
        try:
            from auth_manager import user_can_access_item
            if not user_can_access_item(item_id):
                return jsonify({"ok": False, "error": "无权访问该记录"}), 403
        except Exception:
            pass
        force = body.get("force", True)
        if isinstance(force, str):
            force = force.lower() not in ("0", "false", "no", "off")
        try:
            item_int = int(item_id)
        except Exception:
            return jsonify({"ok": False, "error": "item_id 格式不正确"}), 400
        result = _schedule_existing_lapian(item_int, force=bool(force))
        return jsonify(result), (200 if result.get("ok") else 400)

    def api_bilibili_download():
        item_id = request.args.get("item_id", "").strip()
        kind = request.args.get("kind", "text").strip()
        if not item_id:
            return "缺少 item_id", 400
        try:
            from auth_manager import user_can_access_item
            if not user_can_access_item(item_id):
                return "无权访问该记录", 403
        except Exception:
            pass
        item = _read_item(item_id)
        if not item:
            return "找不到该 B站记录", 404
        return _download_response(item, kind)

    def api_douyin_download_ext():
        kind = request.args.get("kind", "text").strip()
        item_id = request.args.get("item_id", "").strip()
        try:
            from auth_manager import user_can_access_item
            if not user_can_access_item(item_id):
                return "无权访问该记录", 403
        except Exception:
            pass
        if kind == "report":
            return _douyin_material_report_response(item_id)
        if original_douyin_download:
            return original_douyin_download()
        return "抖音下载接口未注册", 404

    def api_extract_delete_ext():
        item_id = (request.json or {}).get("id")
        try:
            from auth_manager import current_user, is_super_admin, unlink_or_delete_item
            user = current_user()
            if is_super_admin(user) and original_extract_delete:
                return original_extract_delete()
            ok, msg = unlink_or_delete_item(item_id, user=user)
            if ok:
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": msg or "记录不存在"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    def api_item_delete_ext():
        item_id = (request.json or {}).get("id")
        try:
            from auth_manager import current_user, is_super_admin, unlink_or_delete_item
            user = current_user()
            if is_super_admin(user) and original_item_delete:
                return original_item_delete()
            ok, msg = unlink_or_delete_item(item_id, user=user)
            if ok:
                return jsonify({"ok": True})
            return jsonify({"ok": False, "error": msg or "记录不存在"}), 404
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    def api_reference_upload():
        files = request.files.getlist("images")
        title = (request.form.get("title") or "").strip()
        if not files:
            return jsonify({"ok": False, "error": "请选择至少一张参考图片"}), 400
        try:
            from reference_prompt import create_reference_upload_task
            from auth_manager import link_item_to_user

            result = create_reference_upload_task(files, title=title)
            if result.get("item_id"):
                link_item_to_user(result["item_id"], action_mode="reverse_prompt")
            return jsonify(result), (200 if result.get("ok") else 400)
        except Exception as e:
            return jsonify({"ok": False, "error": f"参考图反推任务创建失败：{e}"}), 500

    def api_reference_download():
        item_id = request.args.get("item_id", "").strip()
        kind = request.args.get("kind", "text").strip()
        if not item_id:
            return "缺少 item_id", 400
        try:
            from auth_manager import user_can_access_item
            if not user_can_access_item(item_id):
                return "无权访问该记录", 403
        except Exception:
            pass
        item = _read_any_item(item_id)
        if not item or item.get("platform") != "本地上传":
            return "找不到该参考图记录", 404
        return _download_response(item, kind)

    app.view_functions["api_extract"] = api_extract_ext
    if original_list:
        app.view_functions["api_extract_list"] = api_extract_list_ext
    if original_douyin_download:
        app.view_functions["douyin_download"] = api_douyin_download_ext
    if original_extract_delete:
        app.view_functions["api_extract_delete"] = api_extract_delete_ext
    if original_item_delete:
        app.view_functions["api_item_delete"] = api_item_delete_ext
    for endpoint in ("index", "workspace", "douyin_page"):
        if endpoint in app.view_functions:
            app.view_functions[endpoint] = home_redirect
    app.add_url_rule("/api/bilibili/download", "api_bilibili_download", api_bilibili_download)
    app.add_url_rule("/api/extract/lapian", "api_extract_lapian", api_extract_lapian, methods=["POST"])
    app.add_url_rule("/api/reference/upload", "api_reference_upload", api_reference_upload, methods=["POST"])
    app.add_url_rule("/api/reference/download", "api_reference_download", api_reference_download)
