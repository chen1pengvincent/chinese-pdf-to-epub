"""把模型输出收敛为 Pandoc 可安全消费的 Markdown 子集。"""

from __future__ import annotations

import re

_SCRIPT_BLOCK = re.compile(
    r"<\s*(script|style|iframe|object|embed)\b[^>]*>.*?<\s*/\s*\1\s*>",
    flags=re.IGNORECASE | re.DOTALL,
)
_HTML_COMMENT = re.compile(r"<!--.*?-->", flags=re.DOTALL)
_HTML_TAG = re.compile(r"<[^>]+>")
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"(?<!!)\[([^\]]+)\]\([^)]*\)")
# Remove external Markdown reference definitions while preserving footnotes
# such as ``[^1]: ...``.  The old broad expression silently deleted footnotes.
_REFERENCE_LINK = re.compile(r"^\s*\[(?!\^)[^\]]+\]:\s*\S+.*$", flags=re.MULTILINE)
_RAW_BR = re.compile(r"<\s*br\s*/?\s*>", flags=re.IGNORECASE)
_FENCED_CONTAINER = re.compile(r"^\s*:{3,}.*$", flags=re.MULTILINE)


def sanitize_ocr_markdown(text: str) -> str:
    """移除外部资源和任意 HTML，同时尽量保留可见文字。"""
    value = _RAW_BR.sub("  \n", text)
    value = _SCRIPT_BLOCK.sub("", value)
    value = _IMAGE.sub(lambda match: match.group(1), value)
    value = _LINK.sub(lambda match: match.group(1), value)
    value = _REFERENCE_LINK.sub("", value)
    value = _FENCED_CONTAINER.sub("", value)
    value = _HTML_COMMENT.sub("", value)
    value = _HTML_TAG.sub("", value)
    # Braces are meaningful source text in mathematics and code. They remain
    # untouched here; the EPUB builder disables every Pandoc attribute extension
    # and inserts source-page anchors only after this sanitizer has removed HTML.
    value = value.replace("\x00", "")
    # 两个行尾空格是 Markdown hard break，诗歌与由 <br> 规范化出的换行都依赖它。
    lines = [line.rstrip("\r\t") for line in value.splitlines()]
    return "\n".join(lines).strip()
