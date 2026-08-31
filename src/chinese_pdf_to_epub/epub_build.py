"""Build reflowable or hybrid EPUB files through Pandoc.

The normal entry point remains :func:`build_epub`. :func:`build_hybrid_epub`
adds an explicit page manifest: prose pages use reflowable OCR text, while
charts, complex tables, review-only pages, blank pages, and OCR failures retain
the original page image. An image fallback is never counted as OCR success.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from pathlib import Path

from . import epub_verify, post_process
from .ocr import BLANK_PLACEHOLDER, DEAD_PREFIX
from .subprocess_env import safe_subprocess_env

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_ACCESSIBILITY_PREFIX = "schema:access"


def _front_matter_has_field(path: Path, field: str) -> bool:
    """Conservatively detect an explicit scalar in the leading YAML block."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    if not lines or lines[0].strip() != "---":
        return False
    pattern = re.compile(rf"^\s*{re.escape(field)}\s*:")
    for line in lines[1:]:
        if line.strip() in {"---", "..."}:
            return False
        if pattern.match(line):
            return True
    return False


def _sanitize_generated_metadata(
    epub: Path,
    *,
    keep_publication_date: bool,
    keep_accessibility_metadata: bool,
) -> None:
    """Remove Pandoc metadata claims that the pipeline did not verify.

    Pandoc adds the build date as ``dc:date`` even when the source did not state
    a publication date, and emits accessibility claims for generated image
    content. ``dcterms:modified`` is retained because EPUB 3 requires it.
    """
    with zipfile.ZipFile(epub, "r") as source:
        infos = source.infolist()
        entries = {info.filename: source.read(info.filename) for info in infos}
        comment = source.comment

    container_name = "META-INF/container.xml"
    if container_name not in entries:
        return
    try:
        container = ET.fromstring(entries[container_name])
        rootfile = container.find(f".//{{{_CONTAINER_NS}}}rootfile")
        opf_path = (rootfile.get("full-path") or "") if rootfile is not None else ""
        package = ET.fromstring(entries[opf_path])
    except (ET.ParseError, KeyError):
        return  # The structural validator below will report the precise failure.

    metadata = package.find(f"{{{_OPF_NS}}}metadata")
    if metadata is None:
        return
    changed = False
    for child in list(metadata):
        if child.tag == f"{{{_DC_NS}}}date" and not keep_publication_date:
            metadata.remove(child)
            changed = True
            continue
        if (
            child.tag == f"{{{_OPF_NS}}}meta"
            and not keep_accessibility_metadata
            and (child.get("property") or "").startswith(_ACCESSIBILITY_PREFIX)
        ):
            metadata.remove(child)
            changed = True
    if not changed:
        return

    ET.register_namespace("", _OPF_NS)
    ET.register_namespace("dc", _DC_NS)
    entries[opf_path] = ET.tostring(package, encoding="utf-8", xml_declaration=True)

    fd, tmp_name = tempfile.mkstemp(prefix=f".{epub.name}.", suffix=".tmp", dir=epub.parent)
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        with zipfile.ZipFile(tmp, "w") as target:
            target.comment = comment
            for info in infos:
                data = entries[info.filename]
                if info.filename == "mimetype":
                    info.compress_type = zipfile.ZIP_STORED
                target.writestr(info, data)
        os.replace(tmp, epub)
    finally:
        tmp.unlink(missing_ok=True)


def build_epub(
    *,
    input_md: Path,
    output_epub: Path,
    cover: Path | None = None,
    toc_depth: int = 2,
    split_level: int = 1,
    keep_accessibility_metadata: bool = False,
) -> dict:
    """Build one EPUB and fail if its internal structure is invalid."""
    input_md = Path(input_md)
    output_epub = Path(output_epub)
    cover = Path(cover) if cover is not None else None
    if not input_md.is_file():
        raise FileNotFoundError(f"input not found: {input_md}")
    if cover is not None and not cover.is_file():
        raise FileNotFoundError(f"cover not found: {cover}")
    if shutil.which("pandoc") is None:
        raise RuntimeError("pandoc not installed — `brew install pandoc` or apt install pandoc")
    if toc_depth < 1 or split_level < 1:
        raise ValueError("toc_depth and split_level must be positive integers")

    output_epub.parent.mkdir(parents=True, exist_ok=True)
    fd, staged_name = tempfile.mkstemp(
        prefix=f".{output_epub.stem}.", suffix=".epub", dir=output_epub.parent
    )
    os.close(fd)
    staged = Path(staged_name)
    try:
        args = [
            "pandoc", str(input_md), "-o", str(staged),
            "--from", _pandoc_markdown_format(), "--to", "epub", "--toc",
            f"--toc-depth={toc_depth}", f"--split-level={split_level}",
            f"--resource-path={input_md.parent}",
        ]
        if cover is not None:
            args.append(f"--epub-cover-image={cover}")

        result = subprocess.run(
            args, capture_output=True, text=True, check=False, env=safe_subprocess_env()
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"pandoc failed (rc={result.returncode}): {result.stderr.strip()}"
            )
        if not staged.is_file() or staged.stat().st_size == 0:
            raise RuntimeError("pandoc reported success but did not create the EPUB")

        _sanitize_generated_metadata(
            staged,
            keep_publication_date=_front_matter_has_field(input_md, "date"),
            keep_accessibility_metadata=keep_accessibility_metadata,
        )
        validation = epub_verify.validate_epub(staged)
        if not validation["valid"]:
            joined = "; ".join(validation["errors"])
            raise RuntimeError(f"generated EPUB failed structural validation: {joined}")

        magic_ok = True
        if shutil.which("file"):
            check = subprocess.run(
                ["file", str(staged)], capture_output=True, text=True, check=False
                , env=safe_subprocess_env()
            )
            magic_ok = check.returncode == 0 and "EPUB" in check.stdout
        os.replace(staged, output_epub)
    finally:
        staged.unlink(missing_ok=True)

    return {
        "output": str(output_epub),
        "size_bytes": output_epub.stat().st_size,
        "magic_ok": magic_ok,
        "structure_ok": True,
        "validation_errors": [],
        "pandoc_warnings": result.stderr.strip().splitlines() if result.stderr else [],
    }


@dataclass(frozen=True)
class HybridPage:
    """One source page and its independently known OCR/content classification.

    ``page_type``: ``text`` | ``chart`` | ``complex_table`` | ``failed`` | ``review``.
    ``ocr_status``: ``ok`` | ``blank`` | ``failed`` | ``missing``.
    """

    source_page: int
    image: Path
    page_type: str = "text"
    ocr_status: str = "ok"
    ocr_text: str | None = None


_PAGE_TYPES = {"text", "chart", "complex_table", "failed", "review"}
_OCR_STATUSES = {"ok", "blank", "failed", "missing"}
_IMAGE_PAGE_TYPES = {"chart", "complex_table", "failed", "review"}
_LEADING_H1 = re.compile(r"^(# [^\n]+)(?:\n+|$)(.*)$", flags=re.DOTALL)
_UNTRUSTED_ATTRIBUTE_EXTENSIONS = frozenset(
    {
        "bracketed_spans",
        "fenced_divs",
        "header_attributes",
        "table_attributes",
        "inline_code_attributes",
        "fenced_code_attributes",
        "link_attributes",
        "raw_attribute",
        "native_divs",
        "native_spans",
    }
)


def _pandoc_markdown_format() -> str:
    """Disable every attribute extension supported by the installed Pandoc.

    Pandoc versions expose different extension sets (for example Ubuntu's older
    package has no ``table_attributes`` and rejects naming it).  Querying the
    executable prevents a compatibility failure without silently enabling a
    newer attribute grammar.  ``raw_html`` is required only for builder-owned
    source-page spans; model HTML has already been removed.
    """
    result = subprocess.run(
        ["pandoc", "--list-extensions=markdown"],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    if result.returncode != 0:
        raise RuntimeError("pandoc could not report its Markdown extension set")
    supported = {
        line[1:].strip()
        for line in result.stdout.splitlines()
        if len(line) > 1 and line[0] in {"+", "-"}
    }
    if "raw_html" not in supported:
        raise RuntimeError("installed pandoc does not support required raw_html anchors")
    disabled = sorted(_UNTRUSTED_ATTRIBUTE_EXTENSIONS & supported)
    return "markdown+raw_html" + "".join(f"-{name}" for name in disabled)


def _validated_hybrid_pages(
    pages: Sequence[HybridPage], expected_page_numbers: Collection[int] | None
) -> list[HybridPage]:
    ordered = list(pages)
    if not ordered:
        raise ValueError("hybrid EPUB requires at least one page")
    numbers = [page.source_page for page in ordered]
    if any(not isinstance(number, int) or isinstance(number, bool) or number < 1 for number in numbers):
        raise ValueError("source_page values must be positive integers")
    if numbers != sorted(numbers) or len(set(numbers)) != len(numbers):
        raise ValueError("hybrid pages must be unique and sorted by source page")
    if expected_page_numbers is not None:
        raw_expected = list(expected_page_numbers)
        if len(raw_expected) != len(set(raw_expected)):
            raise ValueError("expected_page_numbers contains duplicates")
        expected = sorted(raw_expected)
        if any(
            not isinstance(number, int) or isinstance(number, bool) or number < 1
            for number in expected
        ):
            raise ValueError("expected_page_numbers must contain positive integers")
        if numbers != expected:
            raise ValueError(f"hybrid page conservation failed: expected {expected}, got {numbers}")

    for page in ordered:
        if page.page_type not in _PAGE_TYPES:
            raise ValueError(f"unsupported page_type on page {page.source_page}: {page.page_type}")
        if page.ocr_status not in _OCR_STATUSES:
            raise ValueError(f"unsupported ocr_status on page {page.source_page}: {page.ocr_status}")
        if not Path(page.image).is_file():
            raise FileNotFoundError(f"source image missing for page {page.source_page}: {page.image}")
        text = (page.ocr_text or "").strip()
        if page.ocr_status == "ok":
            if page.page_type in {"failed", "review"}:
                raise ValueError(
                    f"page {page.source_page} cannot be {page.page_type} and OCR-successful"
                )
            if not text or text.startswith(DEAD_PREFIX) or text == BLANK_PLACEHOLDER:
                raise ValueError(f"page {page.source_page} has no verified OCR text")
        elif text and text not in {BLANK_PLACEHOLDER} and not text.startswith(DEAD_PREFIX):
            raise ValueError(
                f"page {page.source_page} has OCR text but status is {page.ocr_status!r}"
            )
    return ordered


def build_hybrid_epub(
    *,
    pages: Sequence[HybridPage],
    output_epub: Path,
    title: str,
    author: str | None = None,
    lang: str = "en",
    year: str | None = None,
    cover: Path | None = None,
    expected_page_numbers: Collection[int] | None = None,
) -> dict:
    """Build an EPUB that preserves non-text/failed pages as original images.

    ``expected_page_numbers`` is the conservation gate. A full-book caller should
    pass its source-page inventory (after any explicit exclusions); a smoke caller
    can pass the exact representative page numbers.
    """
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title must be non-empty")
    if not isinstance(lang, str) or not lang.strip():
        raise ValueError("lang must be non-empty")
    ordered = _validated_hybrid_pages(pages, expected_page_numbers)
    output_epub = Path(output_epub)
    output_epub.parent.mkdir(parents=True, exist_ok=True)

    counts = {
        "source_pages": len(ordered),
        "ocr_pages": 0,
        "image_pages": 0,
        "failed_pages": 0,
        "review_pages": 0,
    }
    with tempfile.TemporaryDirectory(prefix="scan2ebook-hybrid-", dir=output_epub.parent) as tmp:
        stage = Path(tmp)
        assets = stage / "assets"
        assets.mkdir()
        chunks: list[str] = []
        footnote_offset = 0
        for page in ordered:
            image = Path(page.image)
            text = (page.ocr_text or "").strip()
            use_image = page.page_type in _IMAGE_PAGE_TYPES or page.ocr_status != "ok"
            cleaned = ""
            if page.ocr_status == "ok":
                cleaned = post_process.strip_code_fences(
                    post_process.strip_page_image_marker(text)
                ).strip()
                cleaned, page_max = post_process.renumber_footnotes(cleaned, footnote_offset)
                cleaned = post_process.materialize_orphan_footnotes(cleaned, lang)
                footnote_offset += page_max
                cleaned = post_process.upgrade_chapter_headings(cleaned)
                cleaned = post_process._normalize_thematic_breaks(cleaned).strip()
                if not cleaned:
                    raise ValueError(
                        f"page {page.source_page} has no visible OCR text after normalization"
                    )

            anchor = f"source-page-{page.source_page}"
            page_chunks: list[str] = []
            leading = _LEADING_H1.match(cleaned) if cleaned else None
            if leading:
                # Put the trusted span immediately *after* the H1. Keeping it before
                # the heading can move it into the prior split document; putting it
                # inside the H1 makes Pandoc copy the ID into nav.xhtml and violates
                # anchor uniqueness. Model-supplied HTML was removed earlier.
                page_chunks.append(leading.group(1).rstrip())
                page_chunks.append(
                    f'<span id="{anchor}" class="source-page-heading-anchor"></span>'
                )
                cleaned = leading.group(2).strip()
            else:
                # A trusted raw span avoids enabling ``bracketed_spans`` for OCR
                # content. The sanitizer removes every model-supplied HTML tag.
                page_chunks.append(f'<span id="{anchor}"></span>')
            if use_image:
                ext = image.suffix.lower() or ".jpg"
                asset_name = f"source-page-{page.source_page:06d}{ext}"
                shutil.copy2(image, assets / asset_name)
                page_chunks.append(f"![原始扫描页 {page.source_page}](assets/{asset_name})")
                counts["image_pages"] += 1

            if page.ocr_status == "ok":
                if cleaned:
                    page_chunks.append(cleaned)
                counts["ocr_pages"] += 1
            elif page.ocr_status in {"failed", "missing"}:
                counts["failed_pages"] += 1
            if page.page_type == "review":
                counts["review_pages"] += 1
            chunks.append("\n\n".join(page_chunks))

        markdown = stage / "book.hybrid.md"
        front_matter = post_process.build_front_matter(title.strip(), author, lang, year)
        markdown.write_text(front_matter + "\n" + "\n\n".join(chunks) + "\n", encoding="utf-8")
        built = build_epub(
            input_md=markdown,
            output_epub=output_epub,
            cover=cover,
            toc_depth=1,
            split_level=1,
        )

    # The manifest distinguishes image preservation from actual OCR success.
    built["hybrid"] = counts
    built["source_page_numbers"] = [page.source_page for page in ordered]
    return built
