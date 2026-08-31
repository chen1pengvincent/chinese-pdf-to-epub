"""Hybrid EPUB content-preservation and metadata-truthfulness tests."""

from __future__ import annotations

import base64
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest

from chinese_pdf_to_epub import epub_build, epub_verify, ocr

_VALID_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def _image(path: Path, marker: bytes = b"page", data: bytes | None = None) -> Path:
    # Pandoc copies image resources without decoding them; JPEG magic is enough
    # for this structural, network-free test.
    path.write_bytes(data if data is not None else b"\xff\xd8\xff\xe0" + marker + b"\xff\xd9")
    return path


def test_hybrid_builder_keeps_chart_and_failed_page_images(tmp_path):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    images = [_image(tmp_path / f"page_{n:03d}.png", data=_VALID_PNG) for n in range(1, 4)]
    output = tmp_path / "hybrid.epub"
    result = epub_build.build_hybrid_epub(
        pages=[
            epub_build.HybridPage(1, images[0], "text", "ok", "普通正文。"),
            epub_build.HybridPage(2, images[1], "complex_table", "ok", "表格标题。"),
            epub_build.HybridPage(3, images[2], "failed", "failed", ocr.DEAD_PLACEHOLDER.format(reason="x")),
        ],
        output_epub=output,
        title="混合电子书",
        lang="zh-CN",
        expected_page_numbers={1, 2, 3},
    )
    assert result["hybrid"] == {
        "source_pages": 3,
        "ocr_pages": 2,
        "image_pages": 2,
        "failed_pages": 1,
        "review_pages": 0,
    }
    assert epub_verify.validate_epub(output)["valid"] is True
    with zipfile.ZipFile(output) as zf:
        names = zf.namelist()
        # Text-only page 1 is reflowable; table page 2 and failed page 3 keep images.
        assert len([name for name in names if name.endswith(".png")]) == 2
        chapter = zf.read(next(name for name in names if name.endswith("ch001.xhtml")))
        assert "原始扫描页 1".encode() not in chapter
        assert "原始扫描页 2".encode() in chapter
        assert "原始扫描页 3".encode() in chapter
        opf_name = next(name for name in names if name.endswith(".opf"))
        opf = ET.fromstring(zf.read(opf_name))
        opf_ns = "http://www.idpf.org/2007/opf"
        dc_ns = "http://purl.org/dc/elements/1.1/"
        metadata = opf.find(f"{{{opf_ns}}}metadata")
        assert metadata is not None
        assert metadata.find(f"{{{dc_ns}}}date") is None
        properties = [
            meta.get("property", "") for meta in metadata.findall(f"{{{opf_ns}}}meta")
        ]
        assert not any(value.startswith("schema:access") for value in properties)


def test_review_or_dead_placeholder_cannot_be_counted_as_ocr_success(tmp_path):
    image = _image(tmp_path / "page.jpg")
    with pytest.raises(ValueError, match="review and OCR-successful"):
        epub_build.build_hybrid_epub(
            pages=[epub_build.HybridPage(1, image, "review", "ok", "review caption")],
            output_epub=tmp_path / "a.epub",
            title="T",
        )
    with pytest.raises(ValueError, match="no verified OCR text"):
        epub_build.build_hybrid_epub(
            pages=[
                epub_build.HybridPage(
                    1, image, "text", "ok", ocr.DEAD_PLACEHOLDER.format(reason="x")
                )
            ],
            output_epub=tmp_path / "b.epub",
            title="T",
        )


def test_hybrid_builder_fails_closed_on_page_inventory_mismatch(tmp_path):
    image = _image(tmp_path / "page.jpg")
    with pytest.raises(ValueError, match="page conservation failed"):
        epub_build.build_hybrid_epub(
            pages=[epub_build.HybridPage(1, image, "text", "ok", "text")],
            output_epub=tmp_path / "book.epub",
            title="T",
            expected_page_numbers={1, 2},
        )


def test_build_epub_preserves_previous_output_when_validation_fails(tmp_path, monkeypatch):
    markdown = tmp_path / "book.md"
    markdown.write_text("---\ntitle: T\nlang: en\n---\n\ntext\n", encoding="utf-8")
    output = tmp_path / "book.epub"
    output.write_bytes(b"previous-good-output")
    monkeypatch.setattr(epub_build.shutil, "which", lambda _name: "/tool")

    def fake_run(args, **_kwargs):
        staged = Path(args[args.index("-o") + 1])
        staged.write_bytes(b"new-but-invalid")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(epub_build.subprocess, "run", fake_run)
    monkeypatch.setattr(epub_build, "_sanitize_generated_metadata", lambda *_a, **_k: None)
    monkeypatch.setattr(
        epub_build.epub_verify,
        "validate_epub",
        lambda _path: {"valid": False, "errors": ["broken spine"]},
    )
    with pytest.raises(RuntimeError, match="broken spine"):
        epub_build.build_epub(input_md=markdown, output_epub=output)
    assert output.read_bytes() == b"previous-good-output"
    assert not list(tmp_path.glob(".book.*.epub"))
