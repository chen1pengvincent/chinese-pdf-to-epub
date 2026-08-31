"""从页级 OCR 生成保守的中文章节导航候选。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1
_NUM = "0-9〇零一二三四五六七八九十百千万两"
_CHAPTER = re.compile(
    rf"^第[{_NUM}]+(?:章|节|篇|卷|部)(?:\s*[：:、.．\-—]?\s*.*)?$"
)
_NAMED = re.compile(r"^(?:序|序言|前言|引言|绪论|后记|附录|参考文献|索引)(?:\s+.*)?$")
_HEADING = re.compile(r"^\s*#{1,6}(?!#)\s*(\S.*?)\s*$")
_MARKDOWN_META = re.compile(r"([\\`*_{\[\]<>}()#+.!|\-])")


def _validate_title(title: object) -> str:
    if not isinstance(title, str):
        raise RuntimeError("导航标题必须是字符串")
    value = title.strip()
    if not value or len(value) > 200 or len(value.splitlines()) != 1:
        raise RuntimeError("导航标题必须是 1..200 字符的单行纯文本")
    if any(unicodedata.category(char) == "Cc" for char in value):
        raise RuntimeError("导航标题不能包含控制字符")
    return value


def _markdown_text(value: str) -> str:
    """Escape a validated plain-text title before inserting it into Markdown."""
    return _MARKDOWN_META.sub(r"\\\1", value)


def is_chapter_title(value: str) -> bool:
    title = value.strip().strip("#").strip()
    return bool(_CHAPTER.fullmatch(title) or _NAMED.fullmatch(title))


def page_candidates(source_page: int, markdown: str) -> list[dict[str, Any]]:
    lines = markdown.splitlines()
    found: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and is_chapter_title(match.group(1)):
            found.append((index, match.group(1).strip()))
    body_chars = len(re.sub(r"\s+", "", "\n".join(lines)))
    toc_suspect = len(found) >= 2
    result = []
    for index, title in found:
        result.append(
            {
                "source_page": source_page,
                "title": title,
                "line": index + 1,
                "body_chars": body_chars,
                "status": "toc-suspect" if toc_suspect else "candidate",
            }
        )
    return result


def generate(page_texts: dict[int, str]) -> dict[str, Any]:
    candidates = [
        candidate
        for page, text in sorted(page_texts.items())
        for candidate in page_candidates(page, text)
    ]
    by_title: dict[str, list[dict[str, Any]]] = {}
    for item in candidates:
        by_title.setdefault(item["title"], []).append(item)

    entries: list[dict[str, Any]] = []
    for title, matches in by_title.items():
        usable = [item for item in matches if item["status"] == "candidate"]
        if len(usable) == 1:
            item = usable[0]
            entries.append(
                {
                    "source_page": item["source_page"],
                    "title": title,
                    "source": "auto-heading",
                    "status": "verified-candidate",
                }
            )
        elif usable:
            for item in usable:
                item["status"] = "ambiguous-duplicate"
    entries.sort(key=lambda item: item["source_page"])
    return {
        "schema_version": SCHEMA_VERSION,
        "text_fingerprint": _text_fingerprint(page_texts),
        "entries": entries,
        "candidates": candidates,
    }


def _text_fingerprint(page_texts: dict[int, str]) -> str:
    digest = hashlib.sha256()
    for page, text in sorted(page_texts.items()):
        digest.update(str(page).encode("ascii"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
        digest.update(b"\n")
    return digest.hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def save_generated(path: Path, value: dict[str, Any]) -> None:
    _atomic_json(path, value)


def load_or_generate(path: Path, page_texts: dict[int, str]) -> dict[str, Any]:
    expected_fingerprint = _text_fingerprint(page_texts)
    if path.is_file():
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取导航映射 {path}: {exc}") from exc
    else:
        value = generate(page_texts)
        _atomic_json(path, value)
    validate(value, set(page_texts))
    current = value.get("text_fingerprint")
    if current is None:
        value["text_fingerprint"] = expected_fingerprint
        _atomic_json(path, value)
    elif current != expected_fingerprint:
        entries = value.get("entries", [])
        if any(entry.get("source") in {"manual", "recovery-anchor"} for entry in entries):
            raise RuntimeError("OCR 派生文本已变化；请重新核对人工导航后再更新导航映射")
        value = generate(page_texts)
        _atomic_json(path, value)
    return value


def validate(value: dict[str, Any], valid_pages: set[int]) -> None:
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("不支持的导航映射版本")
    entries = value.get("entries")
    if not isinstance(entries, list):
        raise RuntimeError("navigation.entries 必须是数组")
    fingerprint = value.get("text_fingerprint")
    if fingerprint is not None and (
        not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint)
    ):
        raise RuntimeError("navigation.text_fingerprint 必须是 SHA-256")
    pages: set[int] = set()
    titles: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("导航项必须是对象")
        page = entry.get("source_page")
        title = entry.get("title")
        source = entry.get("source")
        if page not in valid_pages:
            raise RuntimeError(f"无效导航项: {entry!r}")
        title = _validate_title(title)
        if source not in {"auto-heading", "manual", "recovery-anchor"}:
            raise RuntimeError(f"导航来源无效: {source!r}")
        if source == "recovery-anchor" and entry.get("status") != "source-start-missing":
            raise RuntimeError("恢复锚点必须显式标记 status=source-start-missing")
        if page in pages or title in titles:
            raise RuntimeError("导航页号和标题必须各自唯一")
        pages.add(page)
        titles.add(title)


def apply_entry(markdown: str, entry: dict[str, Any] | None) -> str:
    if entry is None:
        return markdown
    title = _validate_title(entry["title"])
    rendered_title = _markdown_text(title)
    lines = markdown.splitlines()
    for index, line in enumerate(lines):
        match = _HEADING.match(line)
        if match and match.group(1).strip() == title:
            lines[index] = f"# {rendered_title}"
            return "\n".join(lines)
    note = ""
    if entry.get("source") == "recovery-anchor":
        note = "\n\n*原始章节起始页缺失；此处为首个存续页面的恢复导航锚点。*"
    return f"# {rendered_title}{note}\n\n{markdown}".strip()
