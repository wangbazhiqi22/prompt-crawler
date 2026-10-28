# -*- coding: utf-8 -*-
"""Runtime extensions loaded by Python before the app starts.

This file adds Bilibili link support without editing the existing extractor
module. Python imports ``sitecustomize`` automatically when it is present on
``sys.path``; because the server is launched from this project directory, the
patch is applied before Flask handles requests.
"""

import datetime
import re
from urllib.parse import urlsplit


def _first_url(value: str) -> str:
    match = re.search(r"https?://\S+", value or "")
    if not match:
        return (value or "").strip()
    return match.group(0).rstrip(".,);，。；）")


def _install_bilibili_support():
    try:
        import link_extractor
    except Exception:
        return

    if getattr(link_extractor, "_BILIBILI_SUPPORT_INSTALLED", False):
        return

    platform_name = "B\u7ad9"
    original_detect_platform = link_extractor.detect_platform
    original_extract_link = link_extractor.extract_link

    def detect_platform(url: str) -> str:
        url = _first_url(url)
        host = (urlsplit(url or "").hostname or "").lower()
        if host == "b23.tv" or host.endswith(".b23.tv") or "bilibili.com" in host:
            return platform_name
        return original_detect_platform(url)

    def extract_link(url: str) -> dict:
        return original_extract_link(_first_url(url))

    def normalize_image_url(url: str) -> str:
        url = (url or "").strip()
        if url.startswith("//"):
            return "https:" + url
        return url

    def format_date(value):
        try:
            if value:
                return datetime.datetime.fromtimestamp(int(value)).strftime("%Y-%m-%d")
        except Exception:
            return ""
        return ""

    def parse_bilibili(page) -> dict:
        data = {}
        try:
            data = page.evaluate(
                """() => {
                    const state = window.__INITIAL_STATE__ || {};
                    const video = state.videoData || state.videoInfo || {};
                    const owner = video.owner || state.upData || {};
                    const meta = (name) =>
                        document.querySelector(`meta[property="${name}"]`)?.content ||
                        document.querySelector(`meta[name="${name}"]`)?.content || "";
                    const pickText = (selectors) => {
                        for (const sel of selectors) {
                            const el = document.querySelector(sel);
                            const txt = (el?.getAttribute("title") || el?.innerText || "").trim();
                            if (txt) return txt;
                        }
                        return "";
                    };
                    const tags = Array.from(document.querySelectorAll(
                        ".tag-link, .video-tag-container a, .tag-panel a, .tag-area a"
                    )).map(e => (e.innerText || "").trim()).filter(Boolean);
                    return {
                        title: video.title || meta("og:title") ||
                               pickText(["h1.video-title", "h1[title]", ".video-title", "h1"]) ||
                               document.title || "",
                        author: owner.name || pickText([".up-name", ".username", ".up-info_right .name", ".name"]),
                        desc: video.desc || video.description || meta("description") ||
                              meta("og:description") ||
                              pickText([".desc-info-text", ".video-desc", ".desc-v2 .desc-info-text", ".desc"]),
                        pubdate: video.pubdate || video.ctime || "",
                        pic: video.pic || meta("og:image") || "",
                        tags,
                        stat: video.stat || {}
                    };
                }"""
            ) or {}
        except Exception:
            data = {}

        title = (data.get("title") or page.title() or "").strip()
        author = (data.get("author") or "").strip()
        desc = (data.get("desc") or "").strip()
        tags = [t for t in data.get("tags") or [] if t]
        stat = data.get("stat") or {}

        content_parts = []
        if desc:
            content_parts.append(desc)
        if tags:
            content_parts.append("\u6807\u7b7e\uff1a" + "\u3001".join(tags[:20]))
        if stat:
            stat_parts = []
            for key, label in (
                ("view", "\u64ad\u653e"),
                ("like", "\u70b9\u8d5e"),
                ("coin", "\u6295\u5e01"),
                ("favorite", "\u6536\u85cf"),
                ("share", "\u5206\u4eab"),
            ):
                value = stat.get(key)
                if value not in (None, ""):
                    stat_parts.append(f"{label}{value}")
            if stat_parts:
                content_parts.append("\u6570\u636e\uff1a" + "\uff0c".join(stat_parts))

        image_urls = []
        pic = normalize_image_url(data.get("pic") or "")
        if pic:
            image_urls.append(pic)

        return {
            "title": title.replace("_哔哩哔哩_bilibili", "").strip(),
            "author": author,
            "publish_time": format_date(data.get("pubdate")),
            "content": link_extractor._clean("\n\n".join(content_parts)),
            "image_urls": link_extractor._dedup(image_urls),
        }

    link_extractor.detect_platform = detect_platform
    link_extractor.extract_link = extract_link
    link_extractor._PARSERS[platform_name] = parse_bilibili
    link_extractor._BILIBILI_SUPPORT_INSTALLED = True

    try:
        import link_pipeline

        if not getattr(link_pipeline, "_URL_TEXT_SUPPORT_INSTALLED", False):
            original_analyze_and_store = link_pipeline.analyze_and_store

            def analyze_and_store(url: str, *args, **kwargs):
                return original_analyze_and_store(_first_url(url), *args, **kwargs)

            link_pipeline.analyze_and_store = analyze_and_store
            link_pipeline._URL_TEXT_SUPPORT_INSTALLED = True
    except Exception:
        pass


_install_bilibili_support()
