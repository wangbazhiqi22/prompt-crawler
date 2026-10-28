# -*- coding: utf-8 -*-
"""
提示词结构检测器（免费规则引擎，不调API）。
在采集阶段判断一条内容「像不像含提示词」，把闲聊/广告/纯链接拦在数据库外面。
只拦「非常确定不是提示词」的，拿不准的放行留给 AI 精判。
"""
import re


# ── 提示词特征库 ─────────────────────────────

# AI工具名（出现在内容里说明跟AI提示词强相关）
AI_TOOLS = [
    "midjourney", "stable diffusion", "dall-e", "dalle",
    "chatgpt", "gpt-4", "gpt-3", "claude", "gemini",
    "即梦", "可灵", "文心一格", "通义万相", "秒画",
    "sora", "runway", "pika", "kling", "hailuo",
    "comfyui", "automatic1111", "sd webui",
    "flux", "sdxl", "leonardo", "firefly",
    "cursor", "windsurf", "copilot",
]

# 提示词特有的参数/格式（出现一个就大概率是提示词）
PROMPT_PARAMS = [
    "--ar", "--style", "--seed", "--chaos", "--stylize",
    "--iw", "--no", "--q", "--v", "--s", "--c",
    r"--sref", r"--cref", r"--p", "--weird",
    r"negative prompt", r"steps:", r"cfg", r"sampler",
    r"lora", r"checkpoint", r"upscal",
    r"f/\d", r"\d+mm\b", r"f-\stop",
    r"--ar\s", r"ar\s*\d+:\d+",
    r"16:9", r"9:16", r"3:4", r"4:3", r"1:1", r"2:3",
    r"aspect ratio",
    r"\d+k\b", r"\d+mm lens", r"focal length",
    r"ISO\s*\d+", r"aperture", r"f-stop",
    r"--ar", r"--no", r"--seed",
]

# 提示词模板句式（中文）
CN_TEMPLATE_PATTERNS = [
    r"你是一位.*请.*",
    r"角色[：:].*",
    r"任务[：:].*",
    r"背景[：:].*",
    r"要求[：:].*",
    r"格式[：:].*",
    r"风格[：:].*",
    r"光照[：:].*",
    r"镜头[：:].*",
    r"构图[：:].*",
    r"色调[：:].*",
    r"分辨率[：:].*",
    r"提示词[：:].*",
    r"prompt[：:].*",
    r"咒语[：:].*",
    r"正面[提prompt示词].*",
    r"负面[提prompt示词].*",
    r"步[骤骤].*",
    r"参数[：:].*",
]

# 英文提示词模板特征
EN_TEMPLATE_PATTERNS = [
    r"you are an?\s",
    r"act as (a|an|the)\s",
    r"prompt:",
    r"template:",
    r"system prompt",
    r"role:",
    r"instructions?:",
    r"guidelines?:",
    r"example(s)?:\s",
    r"step \d",
    r"you will\s",
]

# 明显垃圾词（命中且内容短=垃圾）
JUNK_PATTERNS = [
    r"抽[奖送].*关注",
    r"加[群群微信vx].*",
    r"代[充刷].*",
    r"套利",
    r"空投",
    r"giveaway",
    r"airdrop",
    r"follow (me|us)",
    r"free nitro",
    r"限时免费",
    r"扫码",
    r"bit\.ly",
    r"炒币",
    r"燃烧工程师",
    r"^\s*https?://t\.co\S*\s*$",  # 纯链接
]


def strip_links(text: str) -> str:
    return re.sub(r"https?://\S+", "", text or "").strip()


def _score_prompt_indicators(text: str) -> int:
    """给内容打分（基于提示词特征匹配数）。分数越高越像提示词。阈值≥2才入库。"""
    score = 0
    low = text.lower()

    # 去链接后基本空的 → 0分，直接淘汰
    clean = strip_links(text)
    if len(clean) < 15:
        return 0

    # 1) 命中了AI工具名 +2
    for tool in AI_TOOLS:
        if tool.lower() in low:
            score += 2
            break  # 只计一次

    # 2) 命中提示词参数/格式 每个+1（上限4）
    param_hits = 0
    for pat in PROMPT_PARAMS:
        if re.search(pat, text, re.IGNORECASE):
            param_hits += 1
            if param_hits >= 4:
                break
    score += param_hits

    # 3) 命中中文模板句式 每个+1（上限3）
    tmpl_hits = 0
    for pat in CN_TEMPLATE_PATTERNS:
        if re.search(pat, text):
            tmpl_hits += 1
            if tmpl_hits >= 3:
                break
    score += tmpl_hits

    # 4) 命中英文模板句式 每个+1（上限3）
    en_tmpl = 0
    for pat in EN_TEMPLATE_PATTERNS:
        if re.search(pat, text, re.IGNORECASE):
            en_tmpl += 1
            if en_tmpl >= 3:
                break
    score += en_tmpl

    # 5) 内容比较长（>200字）且有工具名，额外 +1
    if len(clean) > 200 and score >= 2:
        score += 1

    return score


def is_junk(text: str) -> bool:
    """是否明显是垃圾（广告/纯链接/引流）。"""
    if not text:
        return True
    clean = strip_links(text)
    if len(clean) < 10:
        return True
    low = text.lower()
    for pat in JUNK_PATTERNS:
        if re.search(pat, text) or (re.search(pat, low)):
            if len(clean) < 120:
                return True
    return False


def looks_like_prompt(text: str) -> tuple:
    """
    检测一条内容是否「像含提示词」。
    返回 (should_keep: bool, score: int, reason: str)

    阈值：score ≥ 2 才入库（宽松——只拦「完全不像」的，拿不准的放行给AI）
    """
    if is_junk(text):
        return False, 0, "明显垃圾/广告/纯链接"
    score = _score_prompt_indicators(text)
    if score >= 2:
        return True, score, f"提示词特征分={score}"
    # 长度>80字 且 不是纯闲聊也放行（避免漏网——AI精判比漏掉强）
    clean = strip_links(text)
    if len(clean) > 120 and score >= 1:
        return True, score, f"较长内容+微弱特征分={score}（放行待AI精判）"
    return False, score, f"特征分不足={score}(需≥2)"
