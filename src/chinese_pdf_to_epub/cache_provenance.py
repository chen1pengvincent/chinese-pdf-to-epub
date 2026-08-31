"""Fail-closed provenance for resumable per-page OCR Markdown caches.

The old resume contract was only "a non-empty ``page_*.md`` exists".  That can
silently mix output produced from another image, model, prompt, context, or PDF
render profile.  This module keeps one atomic manifest beside the Markdown and
validates every cached page before it may be skipped.

Legacy caches are never guessed to be compatible.  An operator may explicitly
adopt them with ``--migrate-legacy-cache``; that records the *current* inputs as
an assertion, not as proof of how the old Markdown was originally produced.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

MANIFEST_NAME = ".scan2ebook-cache-provenance.json"
SOURCE_MANIFEST_NAME = ".scan2ebook-source.json"
SCHEMA_VERSION = 1


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def scan_set_sha256(pages: list[Path]) -> str:
    """Hash ordered page identity and bytes; paths outside the book are omitted."""
    digest = hashlib.sha256()
    for page in pages:
        digest.update(page.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(sha256_file(page).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def _source_profile(input_dir: Path) -> dict:
    """Return stable render/import parameters, explicitly marking unknown history."""
    path = input_dir / SOURCE_MANIFEST_NAME
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {"verified": False, "mode": "unknown-existing-images"}
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid source/render manifest {path}: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(  # noqa: TRY004 - external cache contract violation
            f"invalid source/render manifest structure: {path}"
        )
    return parsed


def build_config(
    *, input_dir: Path, model: str, lang: str | None, prompt_text: str,
    prompt_context: str,
) -> dict:
    return {
        "model": model,
        "lang": str(lang or ""),
        "prompt_sha256": sha256_text(prompt_text),
        "context_sha256": sha256_text(prompt_context),
        "render_profile": _source_profile(input_dir),
    }


def _atomic_write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_manifest(path: Path) -> dict | None:
    if not path.exists():
        return None
    if path.is_symlink():
        raise RuntimeError(f"OCR cache provenance manifest must not be a symlink: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"invalid OCR cache provenance manifest {path}: {exc}") from exc
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != SCHEMA_VERSION
        or not isinstance(value.get("config"), dict)
        or not isinstance(value.get("pages"), dict)
    ):
        raise RuntimeError(f"unsupported OCR cache provenance manifest: {path}")
    return value


def _page_map(pages: list[Path]) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for page in pages:
        if page.stem in result:
            raise RuntimeError(
                f"ambiguous source pages share stem {page.stem!r}; cache filenames would collide"
            )
        result[page.stem] = page
    return result


def _record(page: Path, markdown: Path) -> dict:
    return {
        "source_file": page.name,
        "source_sha256": sha256_file(page),
        "markdown_sha256": sha256_file(markdown),
    }


def _record_with_markdown_hash(page: Path, markdown_sha256: str) -> dict:
    return {
        "source_file": page.name,
        "source_sha256": sha256_file(page),
        "markdown_sha256": markdown_sha256,
    }


def _recover_staged_records(
    output_dir: Path, manifest: dict, page_by_stem: dict[str, Path]
) -> bool:
    changed = False
    for stem, record in list(manifest["pages"].items()):
        if not isinstance(record, dict) or "staged_file" not in record:
            continue
        page = page_by_stem.get(stem)
        staged_name = record.get("staged_file")
        if (
            page is None
            or not isinstance(staged_name, str)
            or Path(staged_name).name != staged_name
            or not staged_name.startswith(f".{stem}.")
            or not staged_name.endswith(".pending")
        ):
            raise RuntimeError(f"invalid staged OCR cache record: {stem}")
        expected = {key: record.get(key) for key in (
            "source_file", "source_sha256", "markdown_sha256"
        )}
        if expected["source_file"] != page.name or expected["source_sha256"] != sha256_file(page):
            raise RuntimeError(f"staged OCR source provenance mismatch: {stem}")
        final = output_dir / f"{stem}.md"
        staged = output_dir / staged_name
        if final.is_file() and sha256_file(final) == expected["markdown_sha256"]:
            staged.unlink(missing_ok=True)
        elif staged.is_file() and not staged.is_symlink() and sha256_file(staged) == expected["markdown_sha256"]:
            os.replace(staged, final)
        else:
            raise RuntimeError(f"staged OCR checkpoint cannot be recovered: {stem}")
        manifest["pages"][stem] = expected
        changed = True
    return changed


def checkpoint_page(
    *, output_dir: Path, page: Path, markdown_text: str, config: dict
) -> Path:
    """Durably checkpoint one paid OCR result and its provenance before continuing."""
    manifest_path = output_dir / MANIFEST_NAME
    current = _read_manifest(manifest_path)
    if current is None or current.get("config") != config:
        raise RuntimeError("OCR cache provenance was not prepared for checkpointing")
    output_dir.mkdir(parents=True, exist_ok=True)
    final = output_dir / f"{page.stem}.md"
    staged = output_dir / f".{page.stem}.{uuid.uuid4().hex}.pending"
    encoded = markdown_text.encode("utf-8")
    with staged.open("xb") as handle:
        os.chmod(staged, 0o600, follow_symlinks=False)
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())
    markdown_sha256 = sha256_file(staged)
    record = _record_with_markdown_hash(page, markdown_sha256)
    current["pages"][page.stem] = {**record, "staged_file": staged.name}
    _atomic_write(manifest_path, current)
    # Do not unconditionally clean up here.  If rename fails, the staged file and
    # its already-durable record must survive.  If the final manifest commit fails,
    # the renamed final file plus that on-disk staged record are recoverable too.
    # Deleting on either path could leave no Markdown bytes for the paid result.
    os.replace(staged, final)
    current["pages"][page.stem] = record
    current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write(manifest_path, current)
    return final


def prepare_cache(
    *, input_dir: Path, output_dir: Path, pages: list[Path], config: dict,
    migrate_legacy: bool = False,
) -> dict:
    """Validate existing cache and return a manifest ready for incremental writes.

    Any unexplained Markdown, missing page record, changed image/Markdown, or run
    configuration drift aborts before network I/O.  The caller may explicitly
    migrate an unmanifested legacy cache, accepting that its historical model and
    prompt cannot be independently proven.
    """
    if output_dir.is_symlink():
        raise RuntimeError(f"OCR cache directory must not be a symlink: {output_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = output_dir / MANIFEST_NAME
    page_by_stem = _page_map(pages)
    candidates = sorted(output_dir.glob("page_*.md"), key=lambda p: p.name)
    if any(path.is_symlink() for path in candidates):
        raise RuntimeError("OCR cache Markdown must not contain symlinks")
    existing_md = [p for p in candidates if p.is_file() and p.stat().st_size > 0]
    manifest = _read_manifest(manifest_path)

    pending_files = {
        path.name
        for path in output_dir.iterdir()
        if path.name.endswith(".pending")
    }
    referenced_pending = {
        record["staged_file"]
        for record in (manifest or {}).get("pages", {}).values()
        if isinstance(record, dict) and isinstance(record.get("staged_file"), str)
    }
    orphan_pending = sorted(pending_files - referenced_pending)
    if orphan_pending:
        raise RuntimeError(
            "unreferenced staged OCR checkpoint(s) require manual review before resume: "
            f"{orphan_pending}"
        )

    if manifest is None:
        if existing_md and not migrate_legacy:
            raise RuntimeError(
                "legacy OCR Markdown cache has no model/prompt/context/image provenance; "
                "move it aside and rerun OCR; this CLI does not silently adopt legacy output"
            )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "config": config,
            "pages": {},
            "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        if existing_md:
            manifest["legacy_migration"] = {
                "operator_asserted": True,
                "warning": "historical model/prompt/context were not independently proven",
            }
            for md in existing_md:
                page = page_by_stem.get(md.stem)
                if page is None:
                    raise RuntimeError(f"cached Markdown has no matching source page: {md}")
                manifest["pages"][md.stem] = _record(page, md)
        _atomic_write(manifest_path, manifest)
        return manifest

    if manifest["config"] != config:
        if existing_md:
            raise RuntimeError(
                "OCR cache provenance mismatch (model, prompt, context, language, or "
                f"render profile changed): {manifest_path}; use a new/empty work directory"
            )
        manifest["config"] = config
        manifest["pages"] = {}
        _atomic_write(manifest_path, manifest)
        return manifest

    if _recover_staged_records(output_dir, manifest, page_by_stem):
        _atomic_write(manifest_path, manifest)
        candidates = sorted(output_dir.glob("page_*.md"), key=lambda p: p.name)
        existing_md = [p for p in candidates if p.is_file() and p.stat().st_size > 0]

    records = manifest["pages"]
    if set(records) != {md.stem for md in existing_md}:
        raise RuntimeError(
            "OCR cache provenance page inventory differs from current Markdown files"
        )
    for md in existing_md:
        page = page_by_stem.get(md.stem)
        record = records.get(md.stem)
        if page is None:
            raise RuntimeError(f"cached Markdown has no matching source page: {md}")
        if not isinstance(record, dict):
            raise RuntimeError(  # noqa: TRY004 - persisted cache contract violation
                f"cached Markdown lacks provenance record: {md}"
            )
        expected = _record(page, md)
        if record != expected:
            raise RuntimeError(
                f"cached page provenance mismatch: {md.name}; source image or Markdown changed"
            )
    return manifest


def finalize_cache(
    *, output_dir: Path, pages: list[Path], config: dict, manifest: dict | None = None,
) -> dict:
    """Atomically record every completed Markdown after an OCR batch."""
    path = output_dir / MANIFEST_NAME
    current = manifest or _read_manifest(path)
    if current is None or current.get("config") != config:
        raise RuntimeError("OCR cache provenance was not prepared for this run")
    records = current["pages"]
    for page in pages:
        md = output_dir / f"{page.stem}.md"
        if md.is_file() and md.stat().st_size > 0:
            records[page.stem] = _record(page, md)
    current["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _atomic_write(path, current)
    return current
