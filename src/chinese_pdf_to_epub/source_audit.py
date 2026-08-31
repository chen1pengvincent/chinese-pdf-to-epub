"""只报告源 PDF 的重复页风险；绝不自动删除或补写页面。"""

from __future__ import annotations

import json
import os
import re
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from . import manifest

SCHEMA_VERSION = 1


def _normalize_text(value: str) -> str:
    value = re.sub(r"<!--.*?-->", "", value, flags=re.DOTALL)
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[^0-9A-Za-z\u3400-\u9fff]", "", value).lower()


def _group_pairs(pairs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not pairs:
        return []
    ordered = sorted(pairs, key=lambda item: (item["first"], item["duplicate"]))
    groups: list[list[dict[str, Any]]] = [[ordered[0]]]
    for item in ordered[1:]:
        previous = groups[-1][-1]
        if item["first"] == previous["first"] + 1 and item["duplicate"] == previous["duplicate"] + 1:
            groups[-1].append(item)
        else:
            groups.append([item])
    return [
        {
            "first_range": [group[0]["first"], group[-1]["first"]],
            "duplicate_range": [group[0]["duplicate"], group[-1]["duplicate"]],
            "similarity_min": round(min(item["similarity"] for item in group), 4),
            "similarity_max": round(max(item["similarity"] for item in group), 4),
            "length": len(group),
        }
        for group in groups
    ]


def audit(
    book_dir: Path,
    page_texts: dict[int, str] | None = None,
    *,
    similarity_threshold: float = 0.985,
    max_offset: int = 64,
    min_chars: int = 80,
) -> dict[str, Any]:
    data = manifest.load(book_dir)
    exact_by_hash: dict[str, list[int]] = {}
    for item in data["pages"]:
        exact_by_hash.setdefault(item["archive_sha256"], []).append(item["source_page"])
    exact = [pages for pages in exact_by_hash.values() if len(pages) > 1]

    pairs: list[dict[str, Any]] = []
    normalized = {
        page: _normalize_text(text)
        for page, text in (page_texts or {}).items()
        if len(_normalize_text(text)) >= min_chars
    }
    numbers = sorted(normalized)
    for left_index, first in enumerate(numbers):
        for duplicate in numbers[left_index + 1 :]:
            offset = duplicate - first
            if offset > max_offset:
                break
            similarity = SequenceMatcher(None, normalized[first], normalized[duplicate], autojunk=False).ratio()
            if similarity >= similarity_threshold:
                pairs.append({"first": first, "duplicate": duplicate, "similarity": similarity})

    findings = {
        "schema_version": SCHEMA_VERSION,
        "policy": "只报告，不自动去重、不重排、不补写缺失内容",
        "source_page_count": data["source_pdf"]["page_count"],
        "included_page_count": len(data["pages"]),
        "exact_duplicate_page_groups": exact,
        "near_duplicate_text_ranges": _group_pairs(pairs),
        "printed_page_gap_detection": {
            "status": "manual-review-required",
            "reason": "正文 OCR 默认忽略页眉页脚和印刷页码，不能据此可靠推断缺页",
        },
    }
    return findings


def write_report(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
