"""页清单：中文 PDF 转换项目的唯一事实源。"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
from typing import Any

SCHEMA_VERSION = 1
MANIFEST_RELATIVE_PATH = Path("work/page-manifest.json")
TERMINAL_OCR_STATES = {"ok", "blank", "unsupported", "failed", "ambiguous"}
REPRESENTATIONS = {"reflow", "hybrid", "image", "pending"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _safe_relative_file(book_dir: Path, raw: object, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw:
        raise RuntimeError(f"{field} 必须是非空相对路径")
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise RuntimeError(f"{field} 不能逃逸书籍目录: {raw!r}")
    root = book_dir.resolve()
    path = root.joinpath(*relative.parts)
    if path.is_symlink():
        raise RuntimeError(f"{field} 不允许使用符号链接: {raw}")
    if not path.is_file():
        raise FileNotFoundError(f"{field} 指向的文件不存在: {raw}")
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"{field} 解析后逃逸书籍目录: {raw!r}")
    return resolved


def validate(data: dict[str, Any], book_dir: Path, *, verify_hashes: bool = True) -> dict[str, Any]:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("不支持的页清单版本")
    source = data.get("source_pdf")
    pages = data.get("pages")
    if not isinstance(source, dict) or not isinstance(pages, list) or not pages:
        raise RuntimeError("页清单缺少 source_pdf 或 pages")

    page_count = source.get("page_count")
    included = source.get("included_pages")
    excluded = source.get("excluded_pages")
    if not isinstance(page_count, int) or page_count < 1:
        raise RuntimeError("source_pdf.page_count 无效")
    if not isinstance(included, list) or not all(isinstance(n, int) for n in included):
        raise RuntimeError("source_pdf.included_pages 无效")
    if included != sorted(set(included)):
        raise RuntimeError("included_pages 必须严格递增且不能重复")
    if not isinstance(excluded, list):
        raise RuntimeError("source_pdf.excluded_pages 无效")
    excluded_numbers: list[int] = []
    for item in excluded:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("page"), int)
            or not isinstance(item.get("reason"), str)
            or not item["reason"].strip()
        ):
            raise RuntimeError("每个排除页都必须有页号和非空理由")
        excluded_numbers.append(item["page"])
    if any(page < 1 or page > page_count for page in excluded_numbers):
        raise RuntimeError("排除页号必须位于 PDF 的 1..page_count 范围")
    expected = [number for number in range(1, page_count + 1) if number not in excluded_numbers]
    if included != expected or len(set(excluded_numbers)) != len(excluded_numbers):
        raise RuntimeError("页清单与 PDF 页数/显式排除页不守恒")

    observed = [item.get("source_page") for item in pages if isinstance(item, dict)]
    if observed != included:
        raise RuntimeError("pages 必须与 included_pages 一一对应并保持源顺序")

    for item in pages:
        if not isinstance(item, dict):
            raise RuntimeError("pages 中存在非对象记录")
        archive = _safe_relative_file(book_dir, item.get("archive_image"), field="archive_image")
        ocr_image = _safe_relative_file(book_dir, item.get("ocr_image"), field="ocr_image")
        if item.get("ocr_state") not in TERMINAL_OCR_STATES | {"pending"}:
            raise RuntimeError(f"第 {item.get('source_page')} 页 OCR 状态无效")
        if item.get("representation") not in REPRESENTATIONS:
            raise RuntimeError(f"第 {item.get('source_page')} 页 representation 无效")
        state = item.get("ocr_state")
        representation = item.get("representation")
        if state == "pending" and representation != "pending":
            raise RuntimeError(f"第 {item.get('source_page')} 页 pending 状态必须对应 pending 表达")
        if state == "ok" and representation not in {"reflow", "hybrid"}:
            raise RuntimeError(f"第 {item.get('source_page')} 页 OCR 成功却没有文本表达")
        if state in TERMINAL_OCR_STATES - {"ok"} and representation != "image":
            raise RuntimeError(f"第 {item.get('source_page')} 页非成功终态必须使用原图表达")
        attempts = item.get("attempts")
        if not isinstance(attempts, int) or attempts < 0 or attempts > 6:
            raise RuntimeError(f"第 {item.get('source_page')} 页 attempts 必须在 0..6")
        if verify_hashes:
            if item.get("archive_sha256") != sha256_file(archive):
                raise RuntimeError(f"第 {item['source_page']} 页存档图哈希不匹配")
            if item.get("ocr_sha256") != sha256_file(ocr_image):
                raise RuntimeError(f"第 {item['source_page']} 页 OCR 图哈希不匹配")
    return data


def load(book_dir: Path, *, verify_hashes: bool = True) -> dict[str, Any]:
    path = book_dir / MANIFEST_RELATIVE_PATH
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取页清单 {path}: {exc}") from exc
    return validate(value, book_dir, verify_hashes=verify_hashes)


def save(book_dir: Path, data: dict[str, Any], *, verify_hashes: bool = True) -> Path:
    validate(data, book_dir, verify_hashes=verify_hashes)
    path = book_dir / MANIFEST_RELATIVE_PATH
    _atomic_json(path, data)
    return path


def page_by_number(data: dict[str, Any]) -> dict[int, dict[str, Any]]:
    return {int(item["source_page"]): item for item in data["pages"]}


def project_path(book_dir: Path, relative: str) -> Path:
    return _safe_relative_file(book_dir, relative, field="project path")
