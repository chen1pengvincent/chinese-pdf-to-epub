from __future__ import annotations

from pathlib import Path

import pytest

from chinese_pdf_to_epub import post_process
from chinese_pdf_to_epub.content_policy import PRESERVE_PAGE_IMAGE_MARKER


def _layout(tmp_path: Path) -> tuple[Path, Path, Path]:
    book = tmp_path / "book"
    ocr_dir = book / "work" / "ocr"
    scans_dir = book / "scans"
    ocr_dir.mkdir(parents=True)
    scans_dir.mkdir(parents=True)
    return ocr_dir, scans_dir, book / "work" / "book.md"


def test_marker_embeds_exact_source_page_and_is_removed(tmp_path):
    ocr_dir, scans_dir, output = _layout(tmp_path)
    (ocr_dir / "page_100.md").write_text(
        f"{PRESERVE_PAGE_IMAGE_MARKER}\n\n图3.4 均值-方差前沿",
        encoding="utf-8",
    )
    (scans_dir / "page_100.jpg").write_bytes(b"jpeg")

    stats = post_process.merge_pages(
        input_dir=ocr_dir,
        output_path=output,
        title="资产管理",
        lang="zh",
    )

    merged = output.read_text(encoding="utf-8")
    assert PRESERVE_PAGE_IMAGE_MARKER not in merged
    assert "../scans/page_100.jpg" in merged
    assert "图3.4 均值-方差前沿" in merged
    assert stats["preserved_images"] == 1


def test_marker_without_source_image_fails_closed(tmp_path):
    ocr_dir, _scans_dir, output = _layout(tmp_path)
    (ocr_dir / "page_100.md").write_text(PRESERVE_PAGE_IMAGE_MARKER, encoding="utf-8")

    with pytest.raises(RuntimeError, match="expected exactly one source image"):
        post_process.merge_pages(
            input_dir=ocr_dir,
            output_path=output,
            title="资产管理",
            lang="zh",
        )


def test_unmarked_page_keeps_reflow_only(tmp_path):
    ocr_dir, scans_dir, output = _layout(tmp_path)
    (ocr_dir / "page_003.md").write_text("资产管理", encoding="utf-8")
    (scans_dir / "page_003.jpg").write_bytes(b"jpeg")

    stats = post_process.merge_pages(
        input_dir=ocr_dir,
        output_path=output,
        title="资产管理",
        lang="zh",
    )

    merged = output.read_text(encoding="utf-8")
    assert "page_003.jpg" not in merged
    assert stats["preserved_images"] == 0


def test_dead_placeholder_becomes_visible_source_image(tmp_path):
    ocr_dir, scans_dir, output = _layout(tmp_path)
    (ocr_dir / "page_009.md").write_text(
        "<!-- OCR FAILED (deterministic) — 需要人工处理 -->", encoding="utf-8"
    )
    (scans_dir / "page_009.jpg").write_bytes(b"source-scan")

    stats = post_process.merge_pages(
        input_dir=ocr_dir,
        output_path=output,
        title="资产管理",
        lang="zh",
    )

    merged = output.read_text(encoding="utf-8")
    assert "../scans/page_009.jpg" in merged
    assert "OCR FAILED" not in merged
    assert stats["preserved_images"] == 1


def test_model_empty_candidate_keeps_source_image(tmp_path):
    ocr_dir, scans_dir, output = _layout(tmp_path)
    (ocr_dir / "page_010.md").write_text("<!-- blank page -->", encoding="utf-8")
    (scans_dir / "page_010.jpg").write_bytes(b"source-scan")

    stats = post_process.merge_pages(
        input_dir=ocr_dir,
        output_path=output,
        title="资产管理",
        lang="zh",
    )

    merged = output.read_text(encoding="utf-8")
    assert "../scans/page_010.jpg" in merged
    assert "blank page" not in merged
    assert stats["preserved_images"] == 1
