# -*- coding: utf-8 -*-
"""Normalize platform share text before routing it to an extractor."""

from __future__ import annotations

import re


# Stop before common rich-text / Markdown wrappers.  The old ``\S+`` pattern
# swallowed ``](https://...)`` when a link copied from chat was pasted into the
# analyser, producing an invalid Douyin URL even though the visible URL itself
# was valid.
_URL_RE = re.compile(
    r'https?://[^\s<>\[\]()"\'，。；：！？、）】》]+',
    re.IGNORECASE,
)
# Douyin has shipped several visually similar copy-command markers.  The
# command itself is the ASCII slug; the marker may be ^^, ˇˇ, ︿︿ or a mix of
# repeated caret/breve characters, followed by decorative arrows.
_DOUYIN_COMMAND_RE = re.compile(
    r"(?:\^\^|ˇˇ|︿︿|﹀﹀|[\^ˇ︿﹀]{2})\s*([A-Za-z0-9_-]{8,40})(?=\s*(?:➝|→|➜|$))",
    re.IGNORECASE,
)
_TRAILING_URL_PUNCTUATION = ".,);，。；）】》]}\"'➝→"


def first_url(text: str) -> str:
    """Return the first HTTP URL in a pasted share message, if present."""
    match = _URL_RE.search(text or "")
    if not match:
        return ""
    return match.group(0).rstrip(_TRAILING_URL_PUNCTUATION)


def extract_douyin_command(text: str) -> str:
    """Extract the token used by new ``^^token➝➝`` Douyin share messages."""
    value = text or ""
    if "抖音" not in value:
        return ""
    match = _DOUYIN_COMMAND_RE.search(value)
    # Some copy surfaces replace the trailing arrows with punctuation or omit
    # them entirely.  Keep a conservative fallback restricted to the marker
    # and slug so ordinary prose is not mistaken for a command.
    if not match:
        match = re.search(r"(?:\^\^|ˇˇ|︿︿|﹀﹀)\s*([A-Za-z0-9_-]{8,40})", value)
    return match.group(1) if match else ""


def is_douyin_share_text(text: str) -> bool:
    """Recognize both regular Douyin URLs and URL-less Douyin command text."""
    url = first_url(text)
    if url and ("douyin.com" in url.lower() or "iesdouyin.com" in url.lower()):
        return True
    return bool(extract_douyin_command(text))


def normalize_douyin_share_text(text: str) -> str:
    """Normalize whitespace while preserving app-only command text.

    ``^^token➝➝`` is an App-only command, not a ``v.douyin.com`` path. It is
    deliberately not converted into a guessed URL because that can point at
    the home page and create a misleading parser failure.
    """
    return (text or "").strip()
