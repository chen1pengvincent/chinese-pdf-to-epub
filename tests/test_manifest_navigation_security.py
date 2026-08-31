from __future__ import annotations

import base64
import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from chinese_pdf_to_epub import manifest, navigation, security_scan, source_audit
from chinese_pdf_to_epub.sanitize import sanitize_ocr_markdown

_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJ"
    "AAAADUlEQVR42mNk+M/wHwAF/gL+3MxZ5wAAAABJRU5ErkJggg=="
)


def make_book(tmp_path: Path, page_count: int = 3) -> tuple[Path, dict]:
    book = tmp_path / "book"
    archive = book / "scans/archive"
    ocr_images = book / "scans/ocr"
    archive.mkdir(parents=True)
    ocr_images.mkdir(parents=True)
    pages = []
    for number in range(1, page_count + 1):
        stored = archive / f"page_{number:06d}.png"
        model = ocr_images / f"page_{number:06d}.png"
        stored.write_bytes(_PNG + bytes([number]))
        model.write_bytes(stored.read_bytes())
        pages.append(
            {
                "source_page": number,
                "archive_image": stored.relative_to(book).as_posix(),
                "archive_sha256": manifest.sha256_file(stored),
                "ocr_image": model.relative_to(book).as_posix(),
                "ocr_sha256": manifest.sha256_file(model),
                "render_mode": "test",
                "ocr_state": "pending",
                "attempts": 0,
                "representation": "pending",
                "text_sha256": None,
                "fallback_reason": None,
            }
        )
    data = {
        "schema_version": 1,
        "project": "chinese-pdf-to-epub",
        "source_pdf": {
            "filename": "synthetic.pdf",
            "sha256": "0" * 64,
            "page_count": page_count,
            "included_pages": list(range(1, page_count + 1)),
            "excluded_pages": [],
            "render": {"mode": "test"},
        },
        "pages": pages,
    }
    manifest.save(book, data)
    return book, data


def test_manifest_rejects_silent_page_loss(tmp_path):
    book, data = make_book(tmp_path)
    data["pages"].pop(1)
    with pytest.raises(RuntimeError, match="一一对应"):
        manifest.save(book, data)


def test_manifest_rejects_changed_image(tmp_path):
    book, data = make_book(tmp_path)
    path = book / data["pages"][0]["archive_image"]
    path.write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="存档图哈希不匹配"):
        manifest.load(book)


def test_navigation_marks_paper_toc_as_suspect():
    result = navigation.generate(
        {
            5: "## 第一章\n## 第二章\n## 第三章",
            20: "## 第一章\n\n这是第一章正文。" * 20,
        }
    )
    assert [entry["source_page"] for entry in result["entries"]] == [20]
    assert sum(item["status"] == "toc-suspect" for item in result["candidates"]) == 3


def test_recovery_navigation_requires_honest_status():
    value = {
        "schema_version": 1,
        "entries": [
            {
                "source_page": 9,
                "title": "第十六章（恢复锚点）",
                "source": "recovery-anchor",
                "status": "normal",
            }
        ],
    }
    with pytest.raises(RuntimeError, match="恢复锚点"):
        navigation.validate(value, {9})


def test_sanitizer_removes_model_generated_resources_and_html():
    raw = (
        "正文<br>下一行\n"
        "![偷读本机](../../private.txt)\n"
        "[远程链接](https://invalid.example/item)\n"
        "<script>bad()</script><iframe src='x'></iframe>"
    )
    cleaned = sanitize_ocr_markdown(raw)
    assert "正文  \n下一行" in cleaned
    assert "偷读本机" in cleaned
    assert "远程链接" in cleaned
    assert "../../" not in cleaned
    assert "https://" not in cleaned
    assert "script" not in cleaned
    assert "iframe" not in cleaned


def test_source_audit_groups_near_duplicate_ranges(tmp_path):
    book, _ = make_book(tmp_path, page_count=4)
    text_a = "甲页独有内容用于重复页检测。" * 20
    text_b = "乙页完全不同的合成中文正文。" * 20
    report = source_audit.audit(
        book,
        {1: text_a, 2: text_b, 3: text_a, 4: text_b},
        max_offset=3,
    )
    ranges = report["near_duplicate_text_ranges"]
    assert any(item["first_range"] == [1, 2] and item["duplicate_range"] == [3, 4] for item in ranges)
    assert report["policy"].startswith("只报告")


def test_security_scan_detects_known_environment_value_without_echoing_it(tmp_path, monkeypatch):
    value = "unit-" + "secret-" + "sentinel"
    monkeypatch.setenv("OPENCODE_GO_API_KEY", value)
    (tmp_path / "note.txt").write_text("prefix " + value + " suffix", encoding="utf-8")
    findings = security_scan.scan_tree(tmp_path)
    assert security_scan.Finding("note.txt", "known-secret-value") in findings
    assert value not in repr(findings)


def test_security_scan_blocks_runtime_artifacts(tmp_path):
    (tmp_path / ".env").write_text("placeholder", encoding="utf-8")
    findings = security_scan.scan_tree(tmp_path, include_known_environment_secrets=False)
    assert security_scan.Finding(".env", "denied-artifact-path") in findings


def _add_tar_file(archive: tarfile.TarFile, name: str, raw: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(raw)
    archive.addfile(info, io.BytesIO(raw))


def test_security_scan_allows_normal_zip_and_tar_directory_entries(tmp_path):
    zip_path = tmp_path / "directories.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("package/", b"")
        archive.writestr("package/readme.txt", b"safe")

    tar_path = tmp_path / "directories.tar"
    with tarfile.open(tar_path, "w") as archive:
        directory = tarfile.TarInfo("package/")
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        _add_tar_file(archive, "package/readme.txt", b"safe")

    findings = security_scan.scan_tree(
        tmp_path,
        include_known_environment_secrets=False,
    )

    assert not any(finding.rule == "unsafe-archive-path" for finding in findings)


def test_security_scan_rejects_duplicate_tar_members(tmp_path):
    tar_path = tmp_path / "duplicate.tar"
    with tarfile.open(tar_path, "w") as archive:
        _add_tar_file(archive, "duplicate.txt", b"first")
        _add_tar_file(archive, "duplicate.txt", b"second")

    findings = security_scan.scan_tree(
        tmp_path,
        include_known_environment_secrets=False,
    )

    assert security_scan.Finding(
        "duplicate.tar",
        "duplicate-archive-member",
    ) in findings


def test_security_scan_rejects_tar_total_expanded_size(tmp_path, monkeypatch):
    monkeypatch.setattr(security_scan, "_MAX_ARCHIVE_TOTAL_BYTES", 7)
    tar_path = tmp_path / "expanded-size.tar"
    with tarfile.open(tar_path, "w") as archive:
        _add_tar_file(archive, "first.txt", b"1234")
        _add_tar_file(archive, "second.txt", b"5678")

    findings = security_scan.scan_tree(
        tmp_path,
        include_known_environment_secrets=False,
    )

    assert security_scan.Finding(
        "expanded-size.tar!second.txt",
        "archive-size-limit",
    ) in findings
