from __future__ import annotations

import base64
import json
from pathlib import Path

from chinese_pdf_to_epub import (
    cache_provenance,
    context_prepass,
    manifest,
    ocr,
    workflow,
)

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def _pending_book(tmp_path: Path) -> tuple[Path, dict]:
    book = tmp_path / "book"
    archive_dir = book / "scans/archive"
    ocr_dir = book / "scans/ocr"
    archive_dir.mkdir(parents=True)
    ocr_dir.mkdir(parents=True)
    archive = archive_dir / "page_000001.png"
    image = ocr_dir / "page_000001.png"
    archive.write_bytes(_PNG)
    image.write_bytes(_PNG)
    data = {
        "schema_version": manifest.SCHEMA_VERSION,
        "project": "chinese-pdf-to-epub",
        "source_pdf": {
            "filename": "synthetic.pdf",
            "sha256": "0" * 64,
            "page_count": 1,
            "included_pages": [1],
            "excluded_pages": [],
            "render": {"mode": "test"},
        },
        "pages": [
            {
                "source_page": 1,
                "archive_image": archive.relative_to(book).as_posix(),
                "archive_sha256": manifest.sha256_file(archive),
                "ocr_image": image.relative_to(book).as_posix(),
                "ocr_sha256": manifest.sha256_file(image),
                "render_mode": "test",
                "ocr_state": "pending",
                "attempts": 0,
                "representation": "pending",
                "text_sha256": None,
                "final_text_sha256": None,
                "fallback_reason": None,
            }
        ],
    }
    manifest.save(book, data)
    return book, data


def test_sixth_success_checkpoint_is_reconciled_before_retry_cap(tmp_path, monkeypatch):
    book, data = _pending_book(tmp_path)
    context = {
        "title": "合成测试书",
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
    context_prepass.save_context(book / "work", context, context_prepass.render_block(context))

    raw_dir = book / "work/ocr/raw"
    pages = [book / data["pages"][0]["ocr_image"]]
    config = cache_provenance.build_config(
        input_dir=book / "scans/ocr",
        model=ocr.DEFAULT_MODEL,
        lang="zh",
        prompt_text=ocr.prompt_for_lang("zh"),
        prompt_context=context_prepass.render_block(context),
    )
    cache_provenance.prepare_cache(
        input_dir=book / "scans/ocr",
        output_dir=raw_dir,
        pages=pages,
        config=config,
    )
    cache_provenance.checkpoint_page(
        output_dir=raw_dir,
        page=pages[0],
        markdown_text="第六次请求成功得到的正文",
        config=config,
    )

    ledger = book / "work/request-ledger.jsonl"
    ledger.write_text(
        "".join(
            json.dumps(
                {"event": "started", "stage": "ocr", "page": pages[0].name}
            )
            + "\n"
            for _ in range(6)
        ),
        encoding="utf-8",
    )

    def unexpected_network_batch(**_kwargs):
        raise AssertionError("verified checkpoint should avoid another OCR request")

    monkeypatch.setattr(ocr, "run_batch", unexpected_network_batch)
    summary = workflow.run_ocr(book, "unused", workers=1)

    page = manifest.load(book)["pages"][0]
    assert page["attempts"] == 6
    assert page["ocr_state"] == "ok"
    assert page["representation"] == "reflow"
    assert page["text_sha256"] == manifest.sha256_file(raw_dir / "page_000001.md")
    assert summary["fail"] == 0
