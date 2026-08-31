from __future__ import annotations

import base64
import json
import shutil
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from chinese_pdf_to_epub import (
    cache_provenance,
    content_policy,
    context_prepass,
    manifest,
    ocr,
    workflow,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def _completed_book(tmp_path: Path) -> tuple[Path, dict]:
    book = tmp_path / "中文 空格书"
    archive_dir = book / "scans/archive"
    ocr_dir = book / "scans/ocr"
    archive_dir.mkdir(parents=True)
    ocr_dir.mkdir(parents=True)
    pages = []
    for number in range(1, 4):
        archive = archive_dir / f"page_{number:06d}.png"
        image = ocr_dir / f"page_{number:06d}.png"
        raw_image = _PNG + bytes([number])
        archive.write_bytes(raw_image)
        image.write_bytes(raw_image)
        state = "ok" if number < 3 else "failed"
        representation = {1: "reflow", 2: "hybrid", 3: "image"}[number]
        pages.append(
            {
                "source_page": number,
                "archive_image": archive.relative_to(book).as_posix(),
                "archive_sha256": manifest.sha256_file(archive),
                "ocr_image": image.relative_to(book).as_posix(),
                "ocr_sha256": manifest.sha256_file(image),
                "render_mode": "synthetic-test",
                "ocr_state": state,
                "attempts": 1 if number < 3 else 6,
                "representation": representation,
                "text_sha256": None,
                "final_text_sha256": None,
                "fallback_reason": None if number < 3 else "合成失败页",
            }
        )
    data = {
        "schema_version": manifest.SCHEMA_VERSION,
        "project": "chinese-pdf-to-epub",
        "source_pdf": {
            "filename": "合成 源文件.pdf",
            "sha256": "0" * 64,
            "page_count": 3,
            "included_pages": [1, 2, 3],
            "excluded_pages": [],
            "render": {"mode": "synthetic-test"},
        },
        "pages": pages,
    }
    manifest.save(book, data)

    context = {
        "title": "合成中文书",
        "author": None,
        "translator": None,
        "publisher": None,
        "year": None,
        "supported_layout": True,
        "unsupported_reason": None,
        "pages_per_image": 1,
        "spread_order": "not-applicable",
        "cover_page": None,
        "content_type": "prose",
        "table_of_contents": [],
        "proper_names": [],
        "terminology": [],
        "layout_notes": None,
        "footnote_convention": None,
        "ocr_pitfalls": [],
        "lang": "zh",
        "_generated_by": ocr.DEFAULT_MODEL,
        **workflow._context_binding(book, data),
    }
    context_block = context_prepass.render_block(context)
    context_prepass.save_context(book / "work", context, context_block)

    images = [book / item["ocr_image"] for item in pages]
    raw_dir = book / "work/ocr/raw"
    config = cache_provenance.build_config(
        input_dir=ocr_dir,
        model=ocr.DEFAULT_MODEL,
        lang="zh",
        prompt_text=ocr.prompt_for_lang("zh"),
        prompt_context=context_block,
    )
    cache_provenance.prepare_cache(
        input_dir=ocr_dir,
        output_dir=raw_dir,
        pages=images,
        config=config,
    )
    raw_texts = {
        1: "# 第一章\n\n正文[^1]。集合 A={1,2}。\n\n[^1]: 第一页脚注",
        2: (
            content_policy.PRESERVE_PAGE_IMAGE_MARKER
            + "\n\n# 第二章\n\n表格说明。"
        ),
    }
    for number, text in raw_texts.items():
        raw = cache_provenance.checkpoint_page(
            output_dir=raw_dir,
            page=images[number - 1],
            markdown_text=text,
            config=config,
        )
        pages[number - 1]["text_sha256"] = manifest.sha256_file(raw)
    manifest.save(book, data)
    return book, data


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_complete_offline_main_chain_preserves_pages_text_images_and_footnotes(tmp_path):
    book, _ = _completed_book(tmp_path)
    output, report = workflow.build_book(book, title="合成中文书")

    assert report["valid"] is True
    assert report["source_pages"] == 3
    assert report["source_page_anchors"] == 3
    assert report["anchors_in_spine"] == [1, 2, 3]
    assert report["preserved_image_hashes_expected"] == 2
    assert report["preserved_image_hashes_found"] == 2
    assert report["security_findings"] == []
    assert output == book / "dist/book.epub"

    with zipfile.ZipFile(output) as archive:
        xhtml_documents = [
            archive.read(name)
            for name in archive.namelist()
            if name.endswith(".xhtml")
        ]
        xhtml = b"\n".join(xhtml_documents).decode("utf-8")
        visible_text = "".join(
            "".join(ET.fromstring(raw).itertext())
            for raw in xhtml_documents
        )
        raster_names = [
            name
            for name in archive.namelist()
            if name.lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp"))
        ]
    assert "第一章" in xhtml and "第二章" in xhtml
    assert "第一页脚注" in xhtml
    # Verify literal braces as visible prose. TeX grouping braces are not visible
    # delimiters, so an inline-math fixture would make this assertion ambiguous.
    assert "A={1,2}" in visible_text
    assert content_policy.PRESERVE_PAGE_IMAGE_MARKER not in xhtml
    assert len(raster_names) == 2

    # A second build with unchanged raw OCR and corrections is an offline,
    # idempotent resume. It must not need an API key or overwrite raw OCR.
    second, second_report = workflow.build_book(book, title="合成中文书")
    assert second == output
    assert second_report["valid"] is True


@pytest.mark.skipif(shutil.which("pandoc") is None, reason="pandoc not installed")
def test_correction_drift_requires_explicit_rederive_and_archives_previous_final(tmp_path):
    book, _ = _completed_book(tmp_path)
    workflow.build_book(book, title="合成中文书")
    corrections = {
        "schema_version": 1,
        "overrides": [],
        "replacements": [
            {
                "page": 1,
                "old": "正文",
                "new": "人工核验正文",
                "expected_count": 1,
            }
        ],
    }
    (book / "work/corrections.json").write_text(
        json.dumps(corrections, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="--rederive-final"):
        workflow.build_book(book, title="合成中文书")

    output, report = workflow.build_book(
        book,
        title="合成中文书",
        force_rederive=True,
    )
    assert report["valid"] is True
    history = book / "work/ocr/final-history"
    assert len([path for path in history.iterdir() if path.is_dir()]) == 1
    with zipfile.ZipFile(output) as archive:
        xhtml = b"\n".join(
            archive.read(name)
            for name in archive.namelist()
            if name.endswith(".xhtml")
        ).decode("utf-8")
    assert "人工核验正文" in xhtml
