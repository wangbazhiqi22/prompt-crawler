# -*- coding: utf-8 -*-
"""文本清洗小工具：繁体转简体、时间码格式化。

本地 ASR 出来的中文经常是繁体（模型训练语料偏台湾/香港），
报告里混着繁体既不符合需求，也会让「乱码感」更重。
这里统一转成简体，并在 opencc 不可用时安全降级（原样返回）。
"""

import re

_converter = None
_unavailable = False


def _get_converter():
    global _converter, _unavailable
    if _converter is not None or _unavailable:
        return _converter
    try:
        from opencc import OpenCC

        # tw2s 比 t2s 多处理"著→着""裡→里"这类一对多字，
        # 本地 ASR 出来的繁体更贴近台湾用法，优先用它。
        for config in ("tw2s", "t2s"):
            try:
                _converter = OpenCC(config)
                break
            except Exception:
                continue
    except Exception:
        _unavailable = True
    if _converter is None:
        _unavailable = True
    return _converter


def to_simplified(text: str) -> str:
    """繁体转简体；没有 opencc 或已是简体时原样返回。"""
    if not text or not isinstance(text, str):
        return text
    # 纯 ASCII 文本没必要过转换器
    if not re.search(r"[\u3400-\u9fff]", text):
        return text
    converter = _get_converter()
    if converter is None:
        return text
    try:
        return converter.convert(text)
    except Exception:
        return text


def normalize_segments(segments):
    """把 ASR 分段列表里的台词统一转简体（就地返回新列表）。"""
    if not segments:
        return segments
    fixed = []
    for segment in segments:
        if isinstance(segment, dict):
            item = dict(segment)
            item["text"] = to_simplified(item.get("text") or "")
            fixed.append(item)
        else:
            fixed.append(segment)
    return fixed
