from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from chinese_pdf_to_epub import cache_provenance


def _pages(tmp_path: Path, n: int = 2) -> tuple[Path, Path, list[Path]]:
    scans = tmp_path / "scans"
    out = tmp_path / "work" / "ocr"
    scans.mkdir(parents=True)
    out.mkdir(parents=True)
    pages = []
    for i in range(1, n + 1):
        page = scans / f"page_{i:03d}.jpg"
        page.write_bytes(f"image-{i}".encode())
        pages.append(page)
    return scans, out, pages


def _config(scans: Path, **over) -> dict:
    values = {
        "input_dir": scans, "model": "fixed-model", "lang": "zh",
        "prompt_text": "prompt-v1", "prompt_context": "context-v1",
    }
    values.update(over)
    return cache_provenance.build_config(**values)


def test_new_cache_records_and_validates_exact_inputs(tmp_path):
    scans, out, pages = _pages(tmp_path)
    config = _config(scans)
    manifest = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    (out / "page_001.md").write_text("正文", encoding="utf-8")
    cache_provenance.finalize_cache(
        output_dir=out, pages=pages, config=config, manifest=manifest
    )

    loaded = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    assert loaded["pages"]["page_001"]["source_sha256"]
    assert loaded["pages"]["page_001"]["markdown_sha256"]
    assert loaded["config"]["render_profile"] == {
        "verified": False, "mode": "unknown-existing-images"
    }


@pytest.mark.parametrize("changed", ["source", "markdown"])
def test_cache_fails_closed_when_page_or_markdown_changes(tmp_path, changed):
    scans, out, pages = _pages(tmp_path)
    config = _config(scans)
    manifest = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    md = out / "page_001.md"
    md.write_text("正文", encoding="utf-8")
    cache_provenance.finalize_cache(
        output_dir=out, pages=pages, config=config, manifest=manifest
    )
    (pages[0] if changed == "source" else md).write_bytes(b"changed")
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        cache_provenance.prepare_cache(
            input_dir=scans, output_dir=out, pages=pages, config=config
        )


def test_cache_fails_closed_when_prompt_context_or_model_changes(tmp_path):
    scans, out, pages = _pages(tmp_path)
    config = _config(scans)
    manifest = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    (out / "page_001.md").write_text("正文", encoding="utf-8")
    cache_provenance.finalize_cache(
        output_dir=out, pages=pages, config=config, manifest=manifest
    )
    changed = _config(scans, model="another-model", prompt_context="context-v2")
    with pytest.raises(RuntimeError, match="provenance mismatch"):
        cache_provenance.prepare_cache(
            input_dir=scans, output_dir=out, pages=pages, config=changed
        )


def test_legacy_cache_requires_explicit_operator_migration(tmp_path):
    scans, out, pages = _pages(tmp_path)
    (out / "page_001.md").write_text("legacy", encoding="utf-8")
    config = _config(scans)
    with pytest.raises(RuntimeError, match="legacy OCR Markdown cache"):
        cache_provenance.prepare_cache(
            input_dir=scans, output_dir=out, pages=pages, config=config
        )

    migrated = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config,
        migrate_legacy=True,
    )
    assert migrated["legacy_migration"]["operator_asserted"] is True
    disk = json.loads((out / cache_provenance.MANIFEST_NAME).read_text())
    assert disk["pages"]["page_001"]["source_sha256"]


def test_source_render_parameters_are_part_of_config(tmp_path):
    scans, _, _ = _pages(tmp_path)
    (scans / cache_provenance.SOURCE_MANIFEST_NAME).write_text(
        '{"schema_version":1,"mode":"pdf-render","dpi":200}', encoding="utf-8"
    )
    assert _config(scans)["render_profile"]["dpi"] == 200


def test_scan_set_hash_binds_page_order_names_and_bytes(tmp_path):
    _, _, pages = _pages(tmp_path)
    before = cache_provenance.scan_set_sha256(pages)
    pages[1].write_bytes(b"changed")
    assert cache_provenance.scan_set_sha256(pages) != before


def test_checkpoint_preserves_staged_result_when_final_rename_fails(
    tmp_path, monkeypatch
):
    scans, out, pages = _pages(tmp_path, n=1)
    config = _config(scans)
    cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    real_replace = os.replace

    def fail_final_rename(source, destination):
        if str(source).endswith(".pending") and Path(destination).suffix == ".md":
            raise OSError("simulated final rename failure")
        return real_replace(source, destination)

    monkeypatch.setattr(cache_provenance.os, "replace", fail_final_rename)
    with pytest.raises(OSError, match="simulated final rename failure"):
        cache_provenance.checkpoint_page(
            output_dir=out,
            page=pages[0],
            markdown_text="已付费 OCR 正文",
            config=config,
        )

    disk = json.loads((out / cache_provenance.MANIFEST_NAME).read_text())
    staged = out / disk["pages"][pages[0].stem]["staged_file"]
    assert staged.read_text(encoding="utf-8") == "已付费 OCR 正文"
    assert not (out / f"{pages[0].stem}.md").exists()

    monkeypatch.setattr(cache_provenance.os, "replace", real_replace)
    recovered = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    assert (out / f"{pages[0].stem}.md").read_text(encoding="utf-8") == "已付费 OCR 正文"
    assert "staged_file" not in recovered["pages"][pages[0].stem]


def test_cache_rejects_unreferenced_pending_checkpoint(tmp_path):
    scans, out, pages = _pages(tmp_path, n=1)
    config = _config(scans)
    cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    (out / ".page_001.orphan.pending").write_text("orphan", encoding="utf-8")

    with pytest.raises(RuntimeError, match="unreferenced staged OCR checkpoint"):
        cache_provenance.prepare_cache(
            input_dir=scans, output_dir=out, pages=pages, config=config
        )


def test_checkpoint_recovers_when_final_manifest_commit_fails(tmp_path, monkeypatch):
    scans, out, pages = _pages(tmp_path, n=1)
    config = _config(scans)
    cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    real_atomic_write = cache_provenance._atomic_write
    calls = 0

    def fail_second_manifest_commit(path, value):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated final manifest commit failure")
        return real_atomic_write(path, value)

    monkeypatch.setattr(cache_provenance, "_atomic_write", fail_second_manifest_commit)
    with pytest.raises(OSError, match="simulated final manifest commit failure"):
        cache_provenance.checkpoint_page(
            output_dir=out,
            page=pages[0],
            markdown_text="最终清单提交前已落盘的正文",
            config=config,
        )

    final = out / f"{pages[0].stem}.md"
    assert final.read_text(encoding="utf-8") == "最终清单提交前已落盘的正文"
    disk = json.loads((out / cache_provenance.MANIFEST_NAME).read_text())
    assert "staged_file" in disk["pages"][pages[0].stem]

    monkeypatch.setattr(cache_provenance, "_atomic_write", real_atomic_write)
    recovered = cache_provenance.prepare_cache(
        input_dir=scans, output_dir=out, pages=pages, config=config
    )
    assert "staged_file" not in recovered["pages"][pages[0].stem]
