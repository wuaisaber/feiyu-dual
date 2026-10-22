"""文本后处理：把模型吐出来的东西收拾成能直接发出去的一条消息。

模型天然爱写 markdown、爱分点、爱解释自己，这三样在群聊和评论区里都很出戏。
"""

from __future__ import annotations

import re
from typing import Iterable, List

_THINK_RE = re.compile(r"<(think|reasoning|analysis)>.*?</\1>", re.S | re.I)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_BOLD_RE = re.compile(r"(\*\*|__)(.*?)\1")
_INLINE_CODE_RE = re.compile(r"`([^`]*)`")
_HEAD_RE = re.compile(r"^\s{0,3}#{1,6}\s*", re.M)
_LIST_RE = re.compile(r"^\s{0,3}([-*+]|\d+[.、)])\s+", re.M)
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?", re.M)
_EMOJI_WRAP_RE = re.compile(r"^[\s\"'“”‘’「」『』]+|[\s\"'“”‘’「」『』]+$")
_SELF_INTRO_RE = re.compile(
    r"^(作为(一个)?(AI|人工智能|语言模型|助手)[，,。]?|我是(一个)?(AI|人工智能|语言模型|助手)[，,。]?)",
    re.I,
)
_MULTI_NL = re.compile(r"\n{2,}")
_SPACES = re.compile(r"[ \t]{2,}")

# 句末标点，截断时优先在这里断
_SENT_END = "。！？!?…~～"


def clean(text: str, max_chars: int = 60, banned: Iterable[str] | None = None,
          strip_markdown: bool = True) -> str:
    if not text:
        return ""
    s = _THINK_RE.sub("", text).strip()

    if strip_markdown:
        s = _MD_LINK_RE.sub(r"\1", s)
        s = _MD_BOLD_RE.sub(r"\2", s)
        s = _INLINE_CODE_RE.sub(r"\1", s)
        s = _HEAD_RE.sub("", s)
        s = _LIST_RE.sub("", s)
        s = _QUOTE_RE.sub("", s)

    s = _MULTI_NL.sub("\n", s)
    s = s.replace("\n", " ")  # 我们发的是"一条消息"，换行在群里很出戏
    s = _SPACES.sub(" ", s)
    s = s.replace("**", "").replace("##", "")
    s = _EMOJI_WRAP_RE.sub("", s)
    s = _SELF_INTRO_RE.sub("", s).strip()
    s = s.replace("\r", "").strip()

    if banned:
        for w in banned:
            w = str(w).strip()
            if w and w in s:
                s = s.replace(w, "*" * len(w))

    return clip(s, max_chars)


def clip(text: str, max_chars: int) -> str:
    """按字符数截断，优先断在句末标点，避免半句话。"""
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    head = text[:max_chars]
    cut = -1
    for i in range(len(head) - 1, max(0, len(head) // 2) - 1, -1):
        if head[i] in _SENT_END:
            cut = i
            break
    if cut > 0:
        head = head[: cut + 1]
    return head.rstrip() or text[:max_chars]


def split_long(text: str, limit: int) -> List[str]:
    """长文拆条（QQ 单条有长度上限时用）。"""
    if len(text) <= limit:
        return [text] if text else []
    out, buf = [], ""
    for ch in text:
        buf += ch
        if len(buf) >= limit or (ch in _SENT_END and len(buf) >= limit // 2):
            out.append(buf.strip())
            buf = ""
    if buf.strip():
        out.append(buf.strip())
    return out


def looks_like_refusal(text: str) -> bool:
    """模型拒绝/道歉式回复，这类不必发出去。"""
    keys = ("作为AI", "作为人工智能", "我不能", "抱歉，我无法", "无法回答")
    return any(k in text for k in keys)
