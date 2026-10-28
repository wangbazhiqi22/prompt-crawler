# -*- coding: utf-8 -*-
"""
多平台链接提取引擎。
输入一个链接，用 Playwright 无头浏览器渲染页面，按平台解析出：
  {platform, title, author, publish_time, content, image_urls, source_url}

支持：微信公众号 / 小红书 / X·Twitter / YouMind / 通用兜底。
（抖音走独立管线 crawler_douyin.py，不在此处理。）

设计：
- 一个 _render() 负责起浏览器+开页+等加载，各平台各写一个 _parse_xxx(page)。
- 国外站(X/YouMind)自动探测本机代理 7890，可达才挂。
- 抓不到正文时抛异常，由上层给出友好提示，不静默返回空。
"""
import html
import json
import re
from browser_support import launch_browser
import socket
import datetime
from urllib.parse import urlsplit

from share_input import first_url, is_douyin_share_text


PROXY = "http://127.0.0.1:7890"


def _now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def _proxy_reachable(proxy_url: str = PROXY) -> bool:
    parsed = urlsplit(proxy_url)
    if not parsed.hostname:
        return False
    port = parsed.port or 80
    try:
        with socket.create_connection((parsed.hostname, port), timeout=0.7):
            return True
    except OSError:
        return False


def _first_url(text: str) -> str:
    return first_url(text) or (text or "").strip()


def detect_platform(url: str) -> str:
    """按 host 判断平台。返回中文平台名。"""
    if is_douyin_share_text(url):
        return "抖音"
    url = _first_url(url)
    host = (urlsplit(url).hostname or "").lower()
    if "mp.weixin.qq.com" in host:
        return "微信公众号"
    if "douyin.com" in host:
        return "抖音"
    if "xiaohongshu.com" in host or "xhslink.com" in host:
        return "小红书"
    if "x.com" in host or "twitter.com" in host:
        return "X"
    if "youmind" in host:
        return "YouMind"
    return "网页"


def _clean(text: str) -> str:
    if not text:
        return ""
    # 压缩多余空行
    lines = [ln.rstrip() for ln in text.replace("\r", "").split("\n")]
    out, blank = [], 0
    for ln in lines:
        if ln.strip():
            out.append(ln)
            blank = 0
        else:
            blank += 1
            if blank <= 1:
                out.append("")
    return "\n".join(out).strip()


def _strip_html_text(value: str) -> str:
    if not value:
        return ""
    try:
        from bs4 import BeautifulSoup

        return _clean(BeautifulSoup(value, "html.parser").get_text("\n", strip=True))
    except Exception:
        text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
        text = re.sub(r"<[^>]+>", "", text)
        return _clean(html.unescape(text))


def _request_session_for_foreign_site():
    import requests

    session = requests.Session()
    if _proxy_reachable():
        session.trust_env = False
        session.proxies = {"http": PROXY, "https": PROXY}
    else:
        # 保留系统代理/TUN 能力；很多客户端不会监听 7890，但会写入系统代理。
        session.trust_env = True
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
        ),
        "Accept": "application/json,text/html,*/*",
    })
    return session


def _extract_x_status(url: str) -> tuple[str, str]:
    path = urlsplit(url).path or ""
    m = re.search(r"/([^/?#]+)/status(?:es)?/(\d+)", path, re.I)
    if not m:
        m = re.search(r"/i/web/status/(\d+)", path, re.I)
        return ("", m.group(1)) if m else ("", "")
    return m.group(1), m.group(2)


def _twitter_url_for_embed(url: str, username: str = "", tweet_id: str = "") -> str:
    if username and tweet_id:
        return f"https://twitter.com/{username}/status/{tweet_id}"
    return url.replace("https://x.com/", "https://twitter.com/").replace("http://x.com/", "https://twitter.com/")


def _pick_first(*values) -> str:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _collect_twimg_urls(node, out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        for key, value in node.items():
            key_l = str(key).lower()
            if isinstance(value, str):
                value_l = value.lower()
                if value_l.endswith((".mp4", ".m3u8")) or "video.twimg.com" in value_l:
                    continue
                if "profile_images" in value_l or "/emoji/" in value_l:
                    continue
                if "pbs.twimg.com" in value_l or "amplify_video_thumb" in value_l or "tweet_video_thumb" in value_l:
                    out.append(value)
                elif key_l in ("thumbnail_url", "image", "image_url") and value.startswith("http"):
                    out.append(value)
            else:
                _collect_twimg_urls(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_twimg_urls(value, out)
    return out


def _collect_x_video_urls(node, out=None):
    if out is None:
        out = []
    if isinstance(node, dict):
        for value in node.values():
            if isinstance(value, str):
                value_l = value.lower()
                if value_l.endswith((".mp4", ".m3u8")) or "video.twimg.com" in value_l:
                    out.append(value)
            else:
                _collect_x_video_urls(value, out)
    elif isinstance(node, list):
        for value in node:
            _collect_x_video_urls(value, out)
    return out


def _parse_x_oembed(payload: dict, url: str, username: str, tweet_id: str) -> dict:
    raw_html = payload.get("html") or ""
    text = _strip_html_text(raw_html)
    content = ""
    publish_time = ""
    if raw_html:
        try:
            from bs4 import BeautifulSoup

            soup = BeautifulSoup(raw_html, "html.parser")
            p = soup.find("p")
            if p:
                content = _clean(p.get_text("\n", strip=True))
            status_link = soup.find("a", href=re.compile(r"/status/\d+"))
            if status_link:
                publish_time = _clean(status_link.get_text(" ", strip=True))
        except Exception:
            pass
    if not content:
        content = re.sub(r"\s+[\u2014-]\s+.*$", "", text, flags=re.S).strip()
    author = _pick_first(payload.get("author_name"), username)
    title = content[:80] if content else f"X 推文 {tweet_id or ''}".strip()
    return {
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "content": _clean(content or text),
        "image_urls": _dedup(_collect_twimg_urls(payload)),
        "video_urls": _dedup(_collect_x_video_urls(payload)),
    }


def _parse_x_alt_json(payload: dict, url: str, username: str, tweet_id: str) -> dict:
    tweet = payload.get("tweet") if isinstance(payload.get("tweet"), dict) else payload
    author_obj = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    user_obj = tweet.get("user") if isinstance(tweet.get("user"), dict) else {}
    content = _pick_first(
        tweet.get("text"),
        tweet.get("full_text"),
        tweet.get("description"),
        tweet.get("message"),
        payload.get("text"),
    )
    author = _pick_first(
        author_obj.get("name"),
        author_obj.get("screen_name"),
        user_obj.get("name"),
        user_obj.get("screen_name"),
        payload.get("user_name"),
        payload.get("author_name"),
        username,
    )
    publish_time = _pick_first(tweet.get("created_at"), tweet.get("date"), payload.get("date"))
    title = content[:80] if content else f"X 推文 {tweet_id or ''}".strip()
    return {
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "content": _clean(content),
        "image_urls": _dedup(_collect_twimg_urls(payload)),
        "video_urls": _dedup(_collect_x_video_urls(payload)),
    }


def _try_x_public_metadata(url: str, username: str, tweet_id: str) -> dict:
    session = _request_session_for_foreign_site()
    twitter_url = _twitter_url_for_embed(url, username, tweet_id)
    candidates = [
        (
            "oembed",
            "https://publish.twitter.com/oembed",
            {"url": twitter_url, "omit_script": "true", "dnt": "true", "lang": "zh-cn"},
        ),
    ]
    if username and tweet_id:
        candidates.extend([
            ("vxtwitter", f"https://api.vxtwitter.com/{username}/status/{tweet_id}", None),
            ("fxtwitter", f"https://api.fxtwitter.com/{username}/status/{tweet_id}", None),
        ])
    if tweet_id:
        candidates.append(("fxtwitter_id", f"https://api.fxtwitter.com/status/{tweet_id}", None))

    last_error = ""
    for kind, endpoint, params in candidates:
        try:
            resp = session.get(endpoint, params=params, timeout=12)
            if resp.status_code >= 400:
                last_error = f"{kind}: HTTP {resp.status_code}"
                continue
            payload = resp.json()
            data = _parse_x_oembed(payload, url, username, tweet_id) if kind == "oembed" else _parse_x_alt_json(payload, url, username, tweet_id)
            if (data.get("content") or data.get("title") or data.get("image_urls")):
                data["raw_external"] = {"provider": kind, "payload": payload}
                return data
        except Exception as e:
            last_error = f"{kind}: {type(e).__name__}: {str(e)[:100]}"
            continue
    if last_error:
        return {"_error": last_error}
    return {}


def _fallback_x_record(url: str, username: str, tweet_id: str, reason: str = "") -> dict:
    title = f"X 推文 {tweet_id}" if tweet_id else "X 链接"
    body = [
        "【X链接已识别】",
        f"原链接：{url}",
    ]
    if username:
        body.append(f"用户：@{username}")
    if tweet_id:
        body.append(f"推文ID：{tweet_id}")
    body.append("暂未抓到正文。通常是 X 需要登录、网络代理不可用，或公开嵌入接口暂时不可达。")
    if reason:
        body.append(f"最后错误：{reason}")
    body.append("建议：开启本机代理 7890 或在浏览器登录 X 后重试；这条记录已先保存，方便后续补采。")
    return {
        "title": title,
        "author": f"@{username}" if username else "",
        "publish_time": "",
        "content": "\n".join(body),
        "image_urls": [],
        "video_urls": [],
        "raw_external": {"provider": "fallback", "error": reason, "tweet_id": tweet_id, "username": username},
    }


def _extract_x_link(url: str) -> dict:
    username, tweet_id = _extract_x_status(url)
    api_data = _try_x_public_metadata(url, username, tweet_id)
    if api_data and (api_data.get("content") or api_data.get("title") or api_data.get("image_urls")):
        return api_data

    try:
        browser_data = _render_and_parse(url, "X")
        if browser_data and (browser_data.get("content") or browser_data.get("title") or browser_data.get("image_urls")):
            return browser_data
    except Exception as e:
        reason = api_data.get("_error") if isinstance(api_data, dict) else ""
        if reason:
            reason += "；"
        reason += str(e)
        return _fallback_x_record(url, username, tweet_id, reason=reason)

    return _fallback_x_record(url, username, tweet_id, reason=api_data.get("_error", "") if isinstance(api_data, dict) else "")


# ---------------- 各平台解析 ----------------
def _parse_wechat(page) -> dict:
    title = ""
    for sel in ["meta[property='og:title']"]:
        try:
            title = page.locator(sel).first.get_attribute("content", timeout=2000) or ""
        except Exception:
            pass
    title = (title or page.title() or "").strip()

    author = ""
    try:
        author = page.evaluate(
            "() => (window.nickname||document.getElementById('js_name')?.innerText||'').trim()"
        ) or ""
    except Exception:
        pass
    if not author:
        try:
            author = page.locator("#js_name").first.inner_text(timeout=2000).strip()
        except Exception:
            pass

    publish_time = ""
    try:
        ct = page.evaluate("() => (typeof ct!=='undefined'? String(ct): '')")
        if ct and ct.isdigit():
            publish_time = datetime.datetime.fromtimestamp(int(ct)).strftime("%Y-%m-%d")
    except Exception:
        pass

    content = ""
    for sel in ["#js_content", "#page-content", ".rich_media_content"]:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                content = el.first.inner_text(timeout=4000)
                if content.strip():
                    break
        except Exception:
            continue

    image_urls = []
    try:
        image_urls = page.eval_on_selector_all(
            "#js_content img, .rich_media_content img",
            "els => els.map(e => e.getAttribute('data-src') || e.src).filter(Boolean)",
        ) or []
    except Exception:
        pass

    return {
        "title": title,
        "author": author,
        "publish_time": publish_time,
        "content": _clean(content),
        "image_urls": _dedup(image_urls),
    }


def _parse_xhs(page) -> dict:
    title = ""
    for sel in ["#detail-title", "meta[property='og:title']"]:
        try:
            loc = page.locator(sel).first
            if sel.startswith("meta"):
                title = loc.get_attribute("content", timeout=2000) or ""
            else:
                title = loc.inner_text(timeout=2000) or ""
            if title.strip():
                break
        except Exception:
            continue
    title = (title or page.title() or "").strip()

    author = ""
    for sel in [".author-wrapper .username", ".username", "a.name"]:
        try:
            author = page.locator(sel).first.inner_text(timeout=2000).strip()
            if author:
                break
        except Exception:
            continue

    content = ""
    for sel in ["#detail-desc", ".note-content", ".desc"]:
        try:
            el = page.locator(sel)
            if el.count() > 0:
                content = el.first.inner_text(timeout=3000)
                if content.strip():
                    break
        except Exception:
            continue

    image_urls = []
    try:
        image_urls = page.eval_on_selector_all(
            ".note-slider-img, .swiper-slide img, meta[property='og:image']",
            "els => els.map(e => e.getAttribute('content') || e.src).filter(Boolean)",
        ) or []
    except Exception:
        pass

    return {
        "title": title,
        "author": author,
        "publish_time": "",
        "content": _clean(content),
        "image_urls": _dedup(image_urls),
    }


def _parse_x(page) -> dict:
    title = (page.title() or "").strip()
    author = ""
    try:
        author = page.locator("div[data-testid='User-Name'] span").first.inner_text(timeout=3000).strip()
    except Exception:
        pass

    content = ""
    try:
        texts = page.eval_on_selector_all(
            "article div[data-testid='tweetText']",
            "els => els.map(e => e.innerText)",
        ) or []
        content = "\n\n".join(texts)
    except Exception:
        pass

    image_urls = []
    try:
        image_urls = page.eval_on_selector_all(
            "article img[src*='twimg']",
            "els => els.map(e => e.src).filter(Boolean)",
        ) or []
    except Exception:
        pass

    return {
        "title": title,
        "author": author,
        "publish_time": "",
        "content": _clean(content),
        "image_urls": _dedup(image_urls),
    }


def _parse_generic(page) -> dict:
    """通用兜底：og:title + <article>/正文最大块 + 主要图片。"""
    title = ""
    try:
        title = page.locator("meta[property='og:title']").first.get_attribute("content", timeout=2000) or ""
    except Exception:
        pass
    title = (title or page.title() or "").strip()

    content = ""
    try:
        content = page.evaluate("""() => {
            const cand = document.querySelector('article') ||
                document.querySelector('main') ||
                document.querySelector('.article, .post, .content, #content');
            if (cand && cand.innerText && cand.innerText.trim().length > 80)
                return cand.innerText;
            // 兜底：挑正文最长的块
            let best='', bestLen=0;
            document.querySelectorAll('div,section').forEach(el=>{
                const t=el.innerText||'';
                if(t.length>bestLen && t.length<20000){best=t;bestLen=t.length;}
            });
            return best || document.body.innerText || '';
        }""")
    except Exception:
        pass

    image_urls = []
    try:
        image_urls = page.eval_on_selector_all(
            "meta[property='og:image'], article img, main img",
            "els => els.map(e => e.getAttribute('content') || e.src).filter(Boolean)",
        ) or []
    except Exception:
        pass

    return {
        "title": title,
        "author": "",
        "publish_time": "",
        "content": _clean(content),
        "image_urls": _dedup(image_urls[:20]),
    }


_PARSERS = {
    "微信公众号": _parse_wechat,
    "小红书": _parse_xhs,
    "X": _parse_x,
    "YouMind": _parse_generic,
    "网页": _parse_generic,
}


def _dedup(urls):
    seen, out = set(), []
    for u in urls or []:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _needs_proxy(platform: str) -> bool:
    return platform in ("X", "YouMind")


def _render_and_parse(url: str, platform: str) -> dict:
    from playwright.sync_api import sync_playwright

    use_proxy = _needs_proxy(platform) and _proxy_reachable()
    launch_kwargs = {"headless": True}
    if use_proxy:
        launch_kwargs["proxy"] = {"server": PROXY}

    with sync_playwright() as p:
        browser = launch_browser(p.chromium,**launch_kwargs)
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            page.goto(url, timeout=45000, wait_until="domcontentloaded")
        except Exception as e:
            browser.close()
            raise RuntimeError(f"打开页面失败：{e}")
        page.wait_for_timeout(2800)
        parser = _PARSERS.get(platform, _parse_generic)
        data = parser(page)
        browser.close()
    return data


def extract_link(url: str) -> dict:
    """统一入口：给一个链接，返回结构化内容 dict。"""
    url = _first_url(url)
    if not url.startswith("http"):
        raise ValueError("请粘贴以 http 开头的有效链接")
    platform = detect_platform(url)
    if platform == "抖音":
        raise RuntimeError("抖音链接请走抖音解析通道（会自动转发）")

    data = _extract_x_link(url) if platform == "X" else _render_and_parse(url, platform)
    if not (data.get("content") or "").strip() and not (data.get("title") or "").strip():
        hint = ""
        if _needs_proxy(platform) and not _proxy_reachable():
            hint = "（该站点在国外，请先开启本机代理 7890 再试）"
        raise RuntimeError(f"没抓到正文内容，可能被反爬拦截或需要登录 {hint}")

    data.update({
        "platform": platform,
        "source_url": url,
        "crawled_at": _now_iso(),
    })
    return data


if __name__ == "__main__":
    import sys, json, io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    u = sys.argv[1] if len(sys.argv) > 1 else "https://mp.weixin.qq.com/s/RSD9nF20GACmQC6gZQlklg"
    d = extract_link(u)
    d["content"] = d["content"][:600] + ("…" if len(d["content"]) > 600 else "")
    print(json.dumps(d, ensure_ascii=False, indent=2))
