from __future__ import annotations

import base64
import shutil
import zipfile
from pathlib import Path

import pytest

from chinese_pdf_to_epub import artifact_audit, epub_build, manifest

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def _book(tmp_path: Path) -> tuple[Path, list[Path]]:
    book = tmp_path / "book"
    archive = book / "scans/archive"
    model = book / "scans/ocr"
    archive.mkdir(parents=True)
    model.mkdir(parents=True)
    images = []
    pages = []
    for number in (1, 2):
        image = archive / f"page_{number:06d}.png"
        image.write_bytes(_PNG + bytes([number]))
        ocr_image = model / image.name
        ocr_image.write_bytes(image.read_bytes())
        images.append(image)
        pages.append(
            {
                "source_page": number,
                "archive_image": image.relative_to(book).as_posix(),
                "archive_sha256": manifest.sha256_file(image),
                "ocr_image": ocr_image.relative_to(book).as_posix(),
                "ocr_sha256": manifest.sha256_file(ocr_image),
                "render_mode": "test",
                "ocr_state": "ok" if number == 1 else "failed",
                "attempts": 1,
                "representation": "reflow" if number == 1 else "image",
                "text_sha256": None,
                "fallback_reason": None if number == 1 else "test",
            }
        )
    manifest.save(
        book,
        {
            "schema_version": 1,
            "project": "chinese-pdf-to-epub",
            "source_pdf": {
                "filename": "synthetic.pdf",
                "sha256": "0" * 64,
                "page_count": 2,
                "included_pages": [1, 2],
                "excluded_pages": [],
                "render": {"mode": "test"},
            },
            "pages": pages,
        },
    )
    return book, images


def _build_test_epub(
    output: Path,
    images: list[Path],
    *,
    page_one_text: str,
) -> None:
    epub_build.build_hybrid_epub(
        pages=[
            epub_build.HybridPage(1, images[0], "text", "ok", page_one_text),
            epub_build.HybridPage(2, images[1], "failed", "failed", None),
        ],
        output_epub=output,
        title="合成测试书",
        lang="zh-CN",
        expected_page_numbers=[1, 2],
    )


def _rewrite_source_xhtml(epub: Path, transform) -> None:
    with zipfile.ZipFile(epub, "r") as source:
        infos = source.infolist()
        entries = {info.filename: source.read(info) for info in infos}
        comment = source.comment
    target = next(
        name
        for name, raw in entries.items()
        if name.lower().endswith((".xhtml", ".html")) and b"source-page-1" in raw
    )
    entries[target] = transform(entries[target])
    staged = epub.with_suffix(".rewritten.epub")
    with zipfile.ZipFile(staged, "w") as rewritten:
        rewritten.comment = comment
        for info in infos:
            rewritten.writestr(info, entries[info.filename])
    staged.replace(epub)


def test_artifact_audit_proves_page_anchors_and_preserved_image_hash(tmp_path):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    book, images = _book(tmp_path)
    output = tmp_path / "book.epub"
    epub_build.build_hybrid_epub(
        pages=[
            epub_build.HybridPage(1, images[0], "text", "ok", "# 第一章\n\n正文。"),
            epub_build.HybridPage(2, images[1], "failed", "failed", None),
        ],
        output_epub=output,
        title="合成测试书",
        lang="zh-CN",
        expected_page_numbers=[1, 2],
    )
    report = artifact_audit.audit_epub(book, output)
    assert report["valid"] is True
    assert report["source_page_anchors"] == 2
    assert report["preserved_image_hashes_found"] == 1


def test_artifact_audit_does_not_treat_visible_code_words_as_markup(tmp_path):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    book, images = _book(tmp_path)
    output = tmp_path / "visible-code.epub"
    _build_test_epub(
        output,
        images,
        page_one_text=(
            "# 第一章\n\n"
            "正文代码说明 onclick= 和 style= 只是文字，"
            "url(https://example.invalid/not-a-css-resource) 也只是文字。"
        ),
    )

    report = artifact_audit.audit_epub(book, output)

    assert report["valid"] is True
    assert report["external_resources"] == []


def test_artifact_audit_rejects_namespaced_active_tag_and_mixed_case_attributes(
    tmp_path,
):
    if shutil.which("pandoc") is None:
        pytest.skip("pandoc not installed")
    book, images = _book(tmp_path)
    output = tmp_path / "active-markup.epub"
    _build_test_epub(output, images, page_one_text="# 第一章\n\n正文。")

    def inject_active_markup(raw: bytes) -> bytes:
        raw = raw.replace(
            b"<body",
            b'<body xmlns:adversary="urn:adversary" '
            b'adversary:OnLoad="alert(1)" STYLE="color:red"',
            1,
        )
        return raw.replace(
            b"</body>",
            b"<adversary:SCRIPT>ignored()</adversary:SCRIPT></body>",
            1,
        )

    _rewrite_source_xhtml(output, inject_active_markup)
    report = artifact_audit.audit_epub(book, output)

    assert report["valid"] is False
    assert any("活性元素" in error for error in report["errors"])
    assert any("事件处理属性" in error for error in report["errors"])
    assert any("内联 style 属性" in error for error in report["errors"])


def test_artifact_audit_missing_and_non_zip_inputs_return_invalid_reports(
    tmp_path,
    monkeypatch,
):
    book, _ = _book(tmp_path)
    monkeypatch.setattr(
        artifact_audit,
        "run_epubcheck",
        lambda *_args, **_kwargs: {
            "available": False,
            "passed": False,
            "command": None,
        },
    )

    missing = tmp_path / "missing.epub"
    missing_report = artifact_audit.audit_epub(book, missing)
    assert missing_report["valid"] is False
    assert missing_report["epub_sha256"] is None

    non_zip = tmp_path / "not-a-zip.epub"
    non_zip.write_bytes(b"this is not a ZIP archive")
    non_zip_report = artifact_audit.audit_epub(book, non_zip)
    assert non_zip_report["valid"] is False
    assert non_zip_report["epub_sha256"] == manifest.sha256_file(non_zip)
