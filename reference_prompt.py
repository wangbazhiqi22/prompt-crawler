# -*- coding: utf-8 -*-
"""参考图片/视频首帧的双版本文生图提示词反推。"""

from __future__ import annotations

import base64
import datetime
import io
import json
import math
import os
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from database import get_conn, insert_item
from link_pipeline import LINK_SOURCE_TAG


ROOT = Path(__file__).resolve().parent
MEDIA_ROOT = ROOT / "douyin_images"
ALLOWED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_UPLOAD_IMAGES = 8
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MJ_MAX_TOTAL_CHARS = 900

MJ_HEADING = "【Midjourney（MJ）英文提示词】"
MJ_PARAMS_HEADING = "【MJ 参数建议】"
GPT_HEADING = "【GPT 文生图中文提示词】"
CHAR_ASSET_HEADING = "【角色资产图 MJ 提示词】"
STORYBOARD_HEADING = "【视频分镜头提示词】"


def _now_iso() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _as_local_url(path: str) -> str:
    p = Path(path)
    return f"/douyin_images/{p.parent.name}/{p.name}"


def _update_item(item_id: int, **fields) -> None:
    conn = get_conn()
    sets = ", ".join(f"{key}=?" for key in fields)
    conn.execute(f"UPDATE items SET {sets} WHERE id=?", [*fields.values(), item_id])
    conn.commit()
    conn.close()


def _image_payload(path: str) -> tuple[str, str, int, int]:
    """生成适合视觉 API 的压缩副本，但不改动用户原图。"""
    from PIL import Image

    with Image.open(path) as source:
        width, height = source.size
        image = source.convert("RGB")
        image.thumbnail((1600, 1600))
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=90, optimize=True)
    return base64.standard_b64encode(buf.getvalue()).decode(), "image/jpeg", width, height


def _aspect_ratio(width: int, height: int) -> str:
    if width <= 0 or height <= 0:
        return "9:16"
    divisor = math.gcd(width, height)
    w, h = width // divisor, height // divisor
    if w > 32 or h > 32:
        ratio = width / height
        common = [(1, 1), (4, 3), (3, 4), (16, 9), (9, 16), (3, 2), (2, 3), (21, 9)]
        w, h = min(common, key=lambda item: abs(item[0] / item[1] - ratio))
    return f"{w}:{h}"


def _single_line_prompt(text: str) -> str:
    value = (text or "").replace("```text", " ").replace("```", " ")
    value = value.replace("**", " ").replace("`", " ")
    value = re.sub(r"^\s*(?:prompt|midjourney prompt|mj prompt)\s*[:：]\s*", "", value, flags=re.I)
    return re.sub(r"\s+", " ", value).strip(" ,;\n\t")


def _safe_mj_params(text: str) -> str:
    """Keep only the conservative parameters supported by the AiMJ input."""
    value = _single_line_prompt(text)
    tokens = []
    patterns = (
        r"--ar\s+\d{1,3}\s*[:/]\s*\d{1,3}",
        r"--style\s+raw\b",
        r"--stylize\s+\d{1,4}\b",
        r"--chaos\s+\d{1,3}\b",
    )
    for pattern in patterns:
        match = re.search(pattern, value, flags=re.I)
        if match:
            token = re.sub(r"\s*[:/]\s*", ":", match.group(0).lower())
            token = re.sub(r"\s+", " ", token)
            tokens.append(token)
    return " ".join(tokens)


def _truncate_mj_body(text: str, limit: int) -> str:
    value = _single_line_prompt(text)
    if len(value) <= limit:
        return value
    candidate = value[:limit].rstrip(" ,;.")
    # Prefer a complete comma-delimited visual phrase near the limit.
    boundary = max(candidate.rfind(","), candidate.rfind(";"), candidate.rfind("."))
    if boundary >= int(limit * 0.72):
        candidate = candidate[:boundary]
    else:
        space = candidate.rfind(" ")
        if space >= int(limit * 0.85):
            candidate = candidate[:space]
    return candidate.rstrip(" ,;.")


def _canonicalize_report_format(text: str) -> str:
    """Repair common Markdown variations before extracting copy-ready fields."""
    value = text or ""
    heading_titles = (
        (MJ_HEADING, "Midjourney（MJ）英文提示词"),
        (MJ_PARAMS_HEADING, "MJ 参数建议"),
        (GPT_HEADING, "GPT 文生图中文提示词"),
        (CHAR_ASSET_HEADING, "角色资产图 MJ 提示词"),
        (STORYBOARD_HEADING, "视频分镜头提示词"),
    )
    for canonical, title in heading_titles:
        value = re.sub(
            rf"(?m)^[ \t]*#{{0,6}}[ \t]*\**[ \t]*(?:【[ \t]*)?"
            rf"{re.escape(title)}(?:[ \t]*】)?[ \t]*\**[ \t]*$",
            canonical,
            value,
        )
    # Models sometimes volunteer a character-count line inside the MJ body.
    # It is metadata, not part of a valid prompt sent to AiMJ.
    value = re.sub(
        r"(?im)^[ \t]*\**[ \t]*字符数[ \t]*[:：][^\n]*$",
        "",
        value,
    )
    field_labels = (
        ("角色锚点", "角色锚点："),
        ("MJ_PROMPT", "MJ_PROMPT: "),
        ("MJ_PARAMS", "MJ_PARAMS: "),
        ("SHOT_PROMPT", "SHOT_PROMPT: "),
    )
    for label, replacement in field_labels:
        value = re.sub(
            rf"(?im)^[ \t]*\**[ \t]*{re.escape(label)}[ \t]*[:：][ \t]*\**[ \t]*",
            replacement,
            value,
        )
    return value


def normalize_reverse_report(text: str) -> str:
    """Normalize the first-frame and character-asset MJ prompts for AiMJ v8."""
    primary_pattern = re.compile(
        rf"({re.escape(MJ_HEADING)}\s*)(.*?)(\s*{re.escape(MJ_PARAMS_HEADING)}\s*)(.*?)"
        rf"(?=\s*{re.escape(GPT_HEADING)})",
        flags=re.S,
    )

    def _safe_pair(body_text: str, params_text: str) -> tuple[str, str]:
        params = _safe_mj_params(params_text)
        body_limit = min(820, MJ_MAX_TOTAL_CHARS - len(params) - (1 if params else 0))
        body = _truncate_mj_body(body_text, max(200, body_limit))
        combined = f"{body} {params}".strip()
        if len(combined) > MJ_MAX_TOTAL_CHARS:
            body = _truncate_mj_body(body, MJ_MAX_TOTAL_CHARS - len(params) - 1)
        return body, params

    def _replace_primary(match: re.Match) -> str:
        body, params = _safe_pair(match.group(2), match.group(4))
        return f"{match.group(1)}{body}\n\n{MJ_PARAMS_HEADING}\n{params}"

    normalized = primary_pattern.sub(_replace_primary, _canonicalize_report_format(text))

    # Character assets use machine-stable labels so the page can expose one
    # independent copy button per role. The parameters must be the final line
    # of each role block, which also makes malformed model output easy to trim.
    asset_pattern = re.compile(
        r"(MJ_PROMPT\s*[:：]\s*)(.*?)(\s*MJ_PARAMS\s*[:：]\s*)(.*?)"
        rf"(?=\s*(?:###\s*角色\s*\d+|{re.escape(STORYBOARD_HEADING)}|"
        r"【负面约束】|【事实与推断边界】|\Z))",
        flags=re.S | re.I,
    )

    def _replace_asset(match: re.Match) -> str:
        body, params = _safe_pair(match.group(2), match.group(4))
        return f"{match.group(1)}{body}\nMJ_PARAMS: {params}\n\n"

    return _canonicalize_report_format(asset_pattern.sub(_replace_asset, normalized))


def _reverse_one_image(path: str, label: str, context: str = "", source_type: str = "image") -> str:
    from local_analysis import reverse_image
    return normalize_reverse_report(reverse_image(path, label, context, source_type))


def generate_dual_prompt_report(
    image_paths: list[str],
    context: str = "",
    source_type: str = "image",
    labels: list[str] | None = None,
    max_images: int = MAX_UPLOAD_IMAGES,
) -> str:
    usable = [str(Path(path)) for path in (image_paths or []) if path and os.path.exists(path)][:max_images]
    if not usable:
        return "（没有可用于提示词反推的参考图。）"
    labels = labels or []
    results = {}
    workers = min(3, len(usable))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="reverse-prompt") as pool:
        futures = {}
        for idx, path in enumerate(usable):
            label = labels[idx] if idx < len(labels) else f"参考图{idx + 1}"
            futures[pool.submit(_reverse_one_image, path, label, context, source_type)] = idx
        for future in as_completed(futures):
            idx = futures[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                results[idx] = f"（参考图{idx + 1}反推失败：{e}）"

    parts = []
    for idx, path in enumerate(usable):
        label = labels[idx] if idx < len(labels) else f"参考图{idx + 1}"
        parts.append(
            f"## {label}\n"
            f"本地素材：{Path(path).name}\n\n"
            f"{results.get(idx) or '（没有返回反推结果。）'}"
        )
    return "\n\n---\n\n".join(parts)


def _save_reference_files(files) -> tuple[str, list[str]]:
    from PIL import Image

    task_key = f"reference_{datetime.datetime.now().strftime('%y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    target_dir = MEDIA_ROOT / task_key
    target_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for file in list(files or [])[:MAX_UPLOAD_IMAGES]:
        filename = str(getattr(file, "filename", "") or "")
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_IMAGE_EXTS:
            continue
        out = target_dir / f"reference_{len(saved) + 1:02d}{ext}"
        file.save(str(out))
        if not out.exists() or out.stat().st_size <= 0 or out.stat().st_size > MAX_UPLOAD_BYTES:
            try:
                out.unlink()
            except Exception:
                pass
            continue
        try:
            with Image.open(out) as image:
                image.verify()
        except Exception:
            try:
                out.unlink()
            except Exception:
                pass
            continue
        saved.append(str(out))
    return task_key, saved


def create_reference_upload_task(files, title: str = "") -> dict:
    task_key, image_paths = _save_reference_files(files)
    if not image_paths:
        return {"ok": False, "error": "没有收到有效图片；支持 JPG、PNG、WEBP，单张不超过 20MB"}

    clean_title = (title or "").strip()[:80] or f"参考图提示词反推（{len(image_paths)}张）"
    image_urls = [_as_local_url(path) for path in image_paths]
    raw = {
        "kind": "reference_upload",
        "analysis_mode": "reverse_prompt",
        "image_paths": image_paths,
        "image_urls": image_urls,
        "reverse_prompt_analysis": "",
    }
    conn = get_conn()
    item = {
        "platform": "本地上传",
        "platform_id": task_key,
        "content_type": "提示词反推",
        "author_name": "",
        "author_id": "",
        "publish_time": _now_iso(),
        "title": clean_title,
        "content": f"正在反推 {len(image_paths)} 张参考图的首帧、角色资产与视频分镜提示词…",
        "media_urls": " ".join(image_urls),
        "like_count": 0,
        "comment_count": 0,
        "share_count": 0,
        "matched_keyword": LINK_SOURCE_TAG,
        "source_url": "",
        "raw_json": json.dumps(raw, ensure_ascii=False),
        "crawled_at": _now_iso(),
        "status": "处理中",
    }
    insert_item(conn, item)
    row = conn.execute(
        "SELECT id FROM items WHERE platform=? AND platform_id=?",
        ("本地上传", task_key),
    ).fetchone()
    conn.close()
    item_id = int(row[0]) if row else 0
    if not item_id:
        return {"ok": False, "error": "创建反推任务失败"}

    def _work():
        try:
            report = generate_dual_prompt_report(
                image_paths,
                context=clean_title,
                source_type="image",
                labels=[f"参考图{i}" for i in range(1, len(image_paths) + 1)],
            )
            content = (
                f"【标题】{clean_title}\n\n"
                f"【素材说明】\n用户上传 {len(image_paths)} 张参考图片，逐图反推首帧 MJ/GPT、角色资产 MJ 与视频分镜头提示词。\n\n"
                f"【文生图/首帧图反推提示词】\n{report}"
            )
            raw["reverse_prompt_analysis"] = report
            _update_item(
                item_id,
                content=content,
                raw_json=json.dumps(raw, ensure_ascii=False),
                status="完成",
            )
            try:
                from analyzer import save_analysis

                conn2 = get_conn()
                save_analysis(conn2, item_id, {
                    "is_prompt": 1,
                    "quality_score": 90,
                    "is_recommended": 1,
                    "category": "文生图反推",
                    "prompt_text": report,
                    "prompt_lang": "en/zh",
                    "summary_zh": f"已从 {len(image_paths)} 张参考图反推首帧图、角色资产图与视频分镜头提示词。",
                    "reason": "根据用户上传参考图逐图进行视觉事实拆解、角色身份锚定与镜头链设计。",
                })
                conn2.close()
            except Exception:
                pass
        except Exception as e:
            _update_item(
                item_id,
                content=f"【反推失败】\n{e}",
                status="失败",
            )

    threading.Thread(target=_work, daemon=True).start()
    return {
        "ok": True,
        "async": True,
        "item_id": item_id,
        "platform": "本地上传",
        "image_count": len(image_paths),
    }
