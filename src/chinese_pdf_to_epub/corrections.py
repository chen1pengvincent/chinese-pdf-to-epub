"""把人工核验后的修订应用到派生层，不修改原始 OCR 缓存。"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import Any

from . import manifest
from .sanitize import sanitize_ocr_markdown

SCHEMA_VERSION = 1


def _page_markdown_name(page: int) -> str:
    """Return the canonical page filename shared by import, OCR, and build."""
    return f"page_{page:06d}.md"


def _final_file_hashes(final_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in sorted(final_dir.glob("page_*.md")):
        if path.is_symlink():
            raise RuntimeError(f"派生 OCR 文本不能是符号链接: {path.name}")
        if path.is_file():
            hashes[path.name] = manifest.sha256_file(path)
    return hashes


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法读取修订配置 {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("修订配置必须是 JSON 对象")
    return value


def _require_local_path(book_dir: Path, path: Path, *, label: str) -> Path:
    root = book_dir.resolve()
    if path.is_symlink():
        raise RuntimeError(f"{label}不能是符号链接")
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError(f"{label}必须位于书籍目录内")
    return resolved


def _safe_override(book_dir: Path, raw: str) -> Path:
    relative = PurePosixPath(raw)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("work", "retries"):
        raise RuntimeError("重试覆盖文件必须位于 work/retries 下")
    root = book_dir.resolve()
    path = root.joinpath(*relative.parts)
    if path.is_symlink():
        raise RuntimeError("重试覆盖文件不能是符号链接")
    if not path.is_file():
        raise FileNotFoundError(f"重试覆盖文件不存在: {raw}")
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise RuntimeError("重试覆盖文件解析后逃逸书籍目录")
    return resolved


def apply(
    book_dir: Path,
    *,
    config_path: Path | None = None,
    raw_dir: Path | None = None,
    final_dir: Path | None = None,
    force_rederive: bool = False,
) -> dict[str, Any]:
    data = manifest.load(book_dir)
    valid_pages = {item["source_page"]: item for item in data["pages"]}
    config_path = config_path or book_dir / "work/corrections.json"
    raw_dir = raw_dir or book_dir / "work/ocr/raw"
    final_dir = final_dir or book_dir / "work/ocr/final"
    config_path = _require_local_path(book_dir, config_path, label="修订配置")
    raw_dir = _require_local_path(book_dir, raw_dir, label="原始 OCR 目录")
    final_dir = _require_local_path(book_dir, final_dir, label="派生 OCR 目录")
    config = (
        _read_json(config_path)
        if config_path.is_file()
        else {"schema_version": SCHEMA_VERSION, "overrides": [], "replacements": []}
    )
    if config.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("不支持的修订配置版本")
    overrides = config.get("overrides", [])
    replacements = config.get("replacements", [])
    if not isinstance(overrides, list) or not isinstance(replacements, list):
        raise RuntimeError("overrides 和 replacements 必须是数组")

    config_bytes = (
        config_path.read_bytes()
        if config_path.is_file()
        else json.dumps(config, ensure_ascii=False, sort_keys=True).encode("utf-8")
    )
    raw_entries = []
    for path in sorted(raw_dir.glob("page_*.md")):
        if path.is_symlink():
            raise RuntimeError(f"原始 OCR 文本不能是符号链接: {path.name}")
        if path.is_file():
            raw_entries.append(f"{path.name}:{manifest.sha256_file(path)}")
    input_fingerprint = manifest.sha256_text(
        manifest.sha256_text(config_bytes.decode("utf-8")) + "\n" + "\n".join(raw_entries)
    )

    existing_report = final_dir / "correction-report.json"
    existing: dict[str, Any] | None = None
    if final_dir.exists():
        if not final_dir.is_dir() or final_dir.is_symlink():
            raise RuntimeError("派生 OCR 路径必须是普通目录")
        if existing_report.is_file():
            try:
                existing = json.loads(existing_report.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = None
            if isinstance(existing, dict) and existing.get("input_fingerprint") == input_fingerprint:
                expected_hashes = existing.get("final_files")
                hashes_match = isinstance(expected_hashes, dict) and expected_hashes == (
                    _final_file_hashes(final_dir)
                )
                if hashes_match:
                    return existing
                if not force_rederive:
                    raise RuntimeError("派生 OCR 文本与修订报告哈希不一致；拒绝继续构建")
        if not force_rederive:
            raise RuntimeError(
                f"拒绝覆盖已有派生目录: {final_dir}；输入已变化，请使用 "
                "`zhpdf2epub build ... --rederive-final` 显式重建并保留旧版本"
            )

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "input_fingerprint": input_fingerprint,
        "overrides": [],
        "replacements": [],
        "sanitized_pages": [],
    }
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ocr-final-", dir=final_dir.parent) as temporary:
        stage = Path(temporary)
        for page_number in sorted(valid_pages):
            source = raw_dir / _page_markdown_name(page_number)
            if source.is_file():
                shutil.copy2(source, stage / source.name)

        seen_overrides: set[int] = set()
        for item in overrides:
            if not isinstance(item, dict):
                raise RuntimeError("覆盖记录必须是对象")
            page = item.get("page")
            if page not in valid_pages or page in seen_overrides:
                raise RuntimeError(f"无效或重复的覆盖页: {page!r}")
            seen_overrides.add(page)
            source = _safe_override(book_dir, str(item.get("markdown", "")))
            if item.get("source_sha256") != valid_pages[page]["archive_sha256"]:
                raise RuntimeError(f"第 {page} 页覆盖记录的源图哈希不匹配")
            if item.get("markdown_sha256") != manifest.sha256_file(source):
                raise RuntimeError(f"第 {page} 页覆盖 Markdown 哈希不匹配")
            target = stage / _page_markdown_name(page)
            before = manifest.sha256_file(target) if target.is_file() else None
            shutil.copy2(source, target)
            report["overrides"].append(
                {"page": page, "before_sha256": before, "after_sha256": manifest.sha256_file(target)}
            )

        for item in replacements:
            if not isinstance(item, dict):
                raise RuntimeError("替换记录必须是对象")
            page = item.get("page")
            old = item.get("old")
            new = item.get("new")
            expected = item.get("expected_count")
            if (
                page not in valid_pages
                or not isinstance(old, str)
                or not old
                or not isinstance(new, str)
                or old == new
                or not isinstance(expected, int)
                or expected < 1
            ):
                raise RuntimeError(f"无效替换记录: {item!r}")
            target = stage / _page_markdown_name(page)
            if not target.is_file():
                raise RuntimeError(f"第 {page} 页没有可修订 OCR 文本")
            text = target.read_text(encoding="utf-8")
            actual = text.count(old)
            if actual != expected:
                raise RuntimeError(
                    f"第 {page} 页替换次数不匹配: expected={expected}, actual={actual}"
                )
            before = manifest.sha256_file(target)
            target.write_text(text.replace(old, new), encoding="utf-8")
            report["replacements"].append(
                {
                    "page": page,
                    "count": actual,
                    "before_sha256": before,
                    "after_sha256": manifest.sha256_file(target),
                }
            )

        for target in sorted(stage.glob("page_*.md")):
            original = target.read_text(encoding="utf-8")
            cleaned = sanitize_ocr_markdown(original)
            if cleaned != original.strip():
                target.write_text(cleaned + ("\n" if cleaned else ""), encoding="utf-8")
                report["sanitized_pages"].append(int(target.stem.rsplit("_", 1)[1]))

        report["final_files"] = _final_file_hashes(stage)

        (stage / "correction-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        if final_dir.exists():
            archive_material = {
                "report": existing,
                "files": _final_file_hashes(final_dir),
            }
            archive_id = manifest.sha256_text(
                json.dumps(archive_material, ensure_ascii=False, sort_keys=True)
            )[:16]
            history = final_dir.parent / "final-history" / archive_id
            history.parent.mkdir(parents=True, exist_ok=True)
            if history.exists():
                raise RuntimeError(f"旧派生版本归档已存在，拒绝覆盖: {history}")
            os.replace(final_dir, history)
            try:
                os.replace(stage, final_dir)
            except Exception:
                os.replace(history, final_dir)
                raise
        else:
            os.replace(stage, final_dir)
    return report
