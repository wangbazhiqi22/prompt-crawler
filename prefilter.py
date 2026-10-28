# -*- coding: utf-8 -*-
"""
免费粗筛：不调用 AI，用规则先把明显不是提示词的垃圾过滤掉。
目的：① 给用户的浏览页减少噪音；② 减少后续 AI 分析的条数，省钱。

判定一条内容是否「明显是垃圾」（返回 True 表示垃圾，应剔除）。
注意：粗筛只敢删「非常明显」的垃圾，把握不准的一律保留，交给 AI 精判。
"""
import re

# 明显的广告/引流特征词（命中且内容短，基本是垃圾）
SPAM_HINTS = [
    "炒币", "燃烧工程师", "套利", "加群", "私信领取", "扫码进群",
    "free nitro", "airdrop", "空投", "giveaway", "follow me",
    "点击链接", "限时免费", "代充", "刷单",
]


def strip_links(text: str) -> str:
    return re.sub(r"https?://\S+", "", text or "").strip()


def is_obvious_junk(content: str) -> bool:
    """只判断『非常明显』的垃圾。拿不准就返回 False（保留）。"""
    if not content:
        return True
    body = strip_links(content)

    # 1) 去掉链接后基本没内容 —— 纯转发链接帖
    if len(body) < 10:
        return True

    # 2) 命中广告词 且 内容不长（长文里偶然出现这些词不算）
    low = body.lower()
    hit_spam = any(h.lower() in low for h in SPAM_HINTS)
    if hit_spam and len(body) < 120:
        return True

    return False


def looks_like_prompt_candidate(content: str) -> bool:
    """
    粗略判断『有可能是提示词』。这只是初筛打标，不是最终判定。
    真正判定交给 AI。这里宽松一点，宁可多留。
    """
    if is_obvious_junk(content):
        return False
    return True
