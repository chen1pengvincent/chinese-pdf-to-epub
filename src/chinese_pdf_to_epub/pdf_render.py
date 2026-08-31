"""Cross-platform PDF -> page-image import.

The public contract stays intentionally small: :func:`render_pdf_to_images`
returns one image for every non-excluded source PDF page, in source order. Missing
a page is data loss, so every strategy is checked against an independently
reported PDF page count before its output is returned.

For scan PDFs, re-rasterising an already-compressed page JPEG loses quality and
costs time. When Poppler reports exactly one ordinary embedded image on a page,
JPEG pages are extracted byte-for-byte with ``pdfimages``. A simple non-JPEG
raster is extracted at its native pixel resolution as PNG. Pages that contain
multiple/complex images or no image are rendered individually with ``pdftoppm``.
This mixed strategy both preserves scan quality
and keeps blank/vector/end-matter pages instead of silently dropping them.

If the direct path cannot prove its invariants, the module falls back to a full
render. ``sips`` remains a last-resort one-page renderer, but page-count
validation prevents its historical multi-page "success with page 1 only" bug.
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from . import ocr
from .subprocess_env import safe_subprocess_env

PDF_SUFFIXES = {".pdf"}

# Kept for CLI compatibility. Directly extracted JPEG pages retain native
# resolution; this DPI applies only to pages that really need rasterisation.
DEFAULT_DPI = 150


def _has(binary: str) -> bool:
    return shutil.which(binary) is not None


def available_backends() -> list[str]:
    """Return full-render backends in preference order (doctor compatibility)."""
    backends: list[str] = []
    if _has("pdftoppm"):
        backends.append("pdftoppm")
    if _has("magick"):
        backends.append("magick")
    if _has("sips"):
        backends.append("sips")
    return backends


def _install_hint() -> str:
    system = platform.system()
    if system == "Windows":
        return (
            "Windows: install Poppler (pdfinfo/pdfimages/pdftoppm) or "
            "ImageMagick + Ghostscript."
        )
    if system == "Linux":
        return "Linux: `sudo apt install poppler-utils` or imagemagick + ghostscript."
    return "macOS: `brew install poppler` or `brew install imagemagick ghostscript`."


_RENDER_PREFIX = "_pdfpage"
_RAW_PREFIX = "_pdfimage"


def _rendered_jpgs(out_dir: Path) -> list[Path]:
    """List importer JPEGs in numeric page order, including page numbers >999."""
    return sorted(out_dir.glob(f"{_RENDER_PREFIX}*.jpg"), key=ocr.natural_sort_key)


def _normalise_source_page_names(pages: list[Path], out_dir: Path) -> list[Path]:
    """Rename backend-specific numbering (including magick's page 0) to 1-based."""
    staged: list[Path] = []
    for index, page in enumerate(pages, 1):
        tmp = out_dir / f"{_RAW_PREFIX}-stage-{index:06d}.jpg"
        page.rename(tmp)
        staged.append(tmp)
    outputs: list[Path] = []
    for index, tmp in enumerate(staged, 1):
        dst = out_dir / f"{_RENDER_PREFIX}-{index:03d}.jpg"
        tmp.rename(dst)
        outputs.append(dst)
    return outputs


def _cleanup_artifacts(out_dir: Path) -> None:
    """Remove only this module's private, regenerable import artifacts."""
    for prefix in (_RENDER_PREFIX, _RAW_PREFIX):
        for path in out_dir.glob(f"{prefix}*"):
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)


def _run_text(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args, capture_output=True, text=True, check=False, env=safe_subprocess_env()
    )


def pdf_page_count(pdf: Path) -> int:
    """Return an independently reported source page count or fail closed.

    ``pdfinfo`` ships with Poppler and is preferred. ``qpdf`` and ImageMagick
    provide conservative fallbacks for installations that render PDFs without
    the rest of Poppler.
    """
    errors: list[str] = []
    if _has("pdfinfo"):
        result = _run_text(["pdfinfo", str(pdf)])
        match = re.search(r"(?m)^Pages:\s+(\d+)\s*$", result.stdout)
        if result.returncode == 0 and match:
            pages = int(match.group(1))
            if pages > 0:
                return pages
        errors.append("pdfinfo did not return a positive Pages value")

    if _has("qpdf"):
        result = _run_text(["qpdf", "--show-npages", str(pdf)])
        if result.returncode == 0 and result.stdout.strip().isdigit():
            pages = int(result.stdout.strip())
            if pages > 0:
                return pages
        errors.append("qpdf did not return a positive page count")

    if _has("magick"):
        # `%n` is the total frame/page count. ImageMagick may print it once per
        # frame, so taking the first positive integer is sufficient.
        result = _run_text(["magick", "identify", "-format", "%n\n", str(pdf)])
        for line in result.stdout.splitlines():
            if result.returncode == 0 and line.strip().isdigit() and int(line) > 0:
                return int(line)
        errors.append("ImageMagick identify did not return a positive page count")

    detail = "; ".join(errors) if errors else "no page-count tool is installed"
    raise RuntimeError(
        f"cannot verify PDF page count for {pdf.name}: {detail}. {_install_hint()}"
    )


@dataclass(frozen=True)
class EmbeddedImage:
    page: int
    number: int
    kind: str
    encoding: str


def _list_embedded_images(pdf: Path) -> list[EmbeddedImage]:
    """Parse ``pdfimages -list`` records; return [] when inspection fails."""
    if not _has("pdfimages"):
        return []
    result = _run_text(["pdfimages", "-list", str(pdf)])
    if result.returncode != 0:
        return []
    records: list[EmbeddedImage] = []
    for line in result.stdout.splitlines():
        fields = line.split()
        # Columns: page num type width height color comp bpc enc ...
        if len(fields) < 9 or not fields[0].isdigit() or not fields[1].isdigit():
            continue
        records.append(
            EmbeddedImage(
                page=int(fields[0]),
                number=int(fields[1]),
                kind=fields[2].lower(),
                encoding=fields[8].lower(),
            )
        )
    return records


_EXTRACTED_NAME = re.compile(r"-(\d+)-(\d+)\.[^.]+$")


def _extracted_page(path: Path) -> int | None:
    match = _EXTRACTED_NAME.search(path.name)
    return int(match.group(1)) if match else None


def _has_jpeg_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"\xff\xd8"
    except OSError:
        return False


def _has_png_magic(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    except OSError:
        return False


def _extract_simple_page_png(pdf: Path, dst: Path, page: int) -> bool:
    """Extract one simple raster page to PNG at the embedded image resolution."""
    try:
        with tempfile.TemporaryDirectory(prefix="scan2ebook-pdfimage-png-") as tmp:
            root = Path(tmp) / "page"
            result = subprocess.run(
                [
                    "pdfimages",
                    "-png",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    str(pdf),
                    str(root),
                ],
                capture_output=True,
                text=True,
                check=False,
                env=safe_subprocess_env(),
            )
            candidates = sorted(Path(tmp).glob("page-*.png"))
            if (
                result.returncode != 0
                or len(candidates) != 1
                or not _has_png_magic(candidates[0])
            ):
                return False
            shutil.copy2(candidates[0], dst)
            return _has_png_magic(dst)
    except (OSError, subprocess.SubprocessError):
        return False


def _render_one_pdftoppm(pdf: Path, dst: Path, page: int, dpi: int) -> bool:
    """Render exactly one source page to one JPEG, with no page-number suffix."""
    result = subprocess.run(
        [
            "pdftoppm", "-f", str(page), "-l", str(page), "-singlefile",
            "-jpeg", "-r", str(dpi), str(pdf), str(dst.with_suffix("")),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    return (
        result.returncode == 0
        and dst.is_file()
        and dst.stat().st_size > 2
        and _has_jpeg_magic(dst)
    )


def _try_direct_scan_import(
    pdf: Path, out_dir: Path, expected_pages: int, dpi: int
) -> list[Path] | None:
    """Try lossless scan import; return None when proof or extraction fails.

    JPEG single-image pages are copied from ``pdfimages -all`` byte-for-byte;
    simple non-JPEG rasters are extracted to native-resolution PNG. Every other
    page is explicitly rendered with ``pdftoppm``. The latter is
    important for a blank/vector page: it remains a page in the EPUB pipeline.
    """
    if not (_has("pdfimages") and _has("pdftoppm")):
        return None
    records = _list_embedded_images(pdf)
    if not records:
        return None

    by_page: dict[int, list[EmbeddedImage]] = {
        page: [] for page in range(1, expected_pages + 1)
    }
    for record in records:
        if record.page not in by_page:
            return None
        by_page[record.page].append(record)

    # This optimisation is for scanned books, not arbitrary mixed/vector PDFs.
    # Require at least 80% of pages to be a single ordinary raster. Otherwise a
    # full renderer is simpler and less surprising.
    simple_pages = {
        page: items[0]
        for page, items in by_page.items()
        if len(items) == 1 and items[0].kind == "image"
    }
    if len(simple_pages) / expected_pages < 0.80:
        return None

    jpeg_pages = {
        page for page, image in simple_pages.items() if image.encoding == "jpeg"
    }
    native_png_pages = set(simple_pages) - jpeg_pages

    try:
        with tempfile.TemporaryDirectory(prefix="scan2ebook-pdfimages-") as tmp:
            root = Path(tmp) / _RAW_PREFIX
            result = subprocess.run(
                ["pdfimages", "-all", "-p", str(pdf), str(root)],
                capture_output=True,
                text=True,
                check=False,
                env=safe_subprocess_env(),
            )
            if result.returncode != 0:
                return None

            extracted_by_page: dict[int, list[Path]] = {}
            for path in Path(tmp).glob(f"{_RAW_PREFIX}-*"):
                page = _extracted_page(path)
                if page is not None:
                    extracted_by_page.setdefault(page, []).append(path)

            outputs: list[Path] = []
            for page in range(1, expected_pages + 1):
                extracted = extracted_by_page.get(page, [])
                if page in jpeg_pages:
                    dst = out_dir / f"{_RENDER_PREFIX}-{page:03d}.jpg"
                    jpgs = [p for p in extracted if p.suffix.lower() in {".jpg", ".jpeg"}]
                    if len(jpgs) != 1 or not _has_jpeg_magic(jpgs[0]):
                        return None
                    shutil.copy2(jpgs[0], dst)
                elif page in native_png_pages:
                    dst = out_dir / f"{_RENDER_PREFIX}-{page:03d}.png"
                    if not _extract_simple_page_png(pdf, dst, page):
                        return None
                else:
                    dst = out_dir / f"{_RENDER_PREFIX}-{page:03d}.jpg"
                    if not _render_one_pdftoppm(pdf, dst, page, dpi):
                        return None
                outputs.append(dst)

            if len(outputs) != expected_pages or not all(p.is_file() for p in outputs):
                return None
            return outputs
    except (OSError, subprocess.SubprocessError):
        return None


def _render_pdftoppm(pdf: Path, out_dir: Path, dpi: int) -> bool:
    result = subprocess.run(
        ["pdftoppm", "-jpeg", "-r", str(dpi), str(pdf), str(out_dir / _RENDER_PREFIX)],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    return result.returncode == 0 and bool(_rendered_jpgs(out_dir))


def _render_magick(pdf: Path, out_dir: Path, dpi: int) -> bool:
    out_pattern = str(out_dir / f"{_RENDER_PREFIX}-%03d.jpg")
    result = subprocess.run(
        ["magick", "-density", str(dpi), str(pdf), "-quality", "92", out_pattern],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    return result.returncode == 0 and bool(_rendered_jpgs(out_dir))


def _render_sips(pdf: Path, out_dir: Path, dpi: int) -> bool:
    del dpi  # sips does not provide a reliable PDF raster DPI option.
    dst = out_dir / f"{_RENDER_PREFIX}-001.jpg"
    result = subprocess.run(
        ["sips", "-s", "format", "jpeg", str(pdf), "--out", str(dst)],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    return result.returncode == 0 and dst.exists()


_RENDERERS = {
    "pdftoppm": _render_pdftoppm,
    "magick": _render_magick,
    "sips": _render_sips,
}


def render_pdf_to_images(
    pdf: Path,
    out_dir: Path,
    dpi: int = DEFAULT_DPI,
    *,
    excluded_pages: set[int] | frozenset[int] | None = None,
) -> list[Path]:
    """Import one image per non-excluded PDF page and enforce conservation.

    ``excluded_pages`` is an explicit, 1-based source-page policy. Output file
    names retain those source page numbers, so excluding an interior page never
    renumbers later pages. The current pipeline calls this function without an
    exclusion policy and therefore requires exactly one output per PDF page.
    """
    pdf = Path(pdf)
    out_dir = Path(out_dir)
    if not pdf.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf}")
    if not isinstance(dpi, int) or isinstance(dpi, bool) or dpi <= 0:
        raise ValueError(f"dpi must be a positive integer, got {dpi!r}")
    out_dir.mkdir(parents=True, exist_ok=True)

    backends = available_backends()
    if not backends and not (_has("pdfimages") and _has("pdftoppm")):
        raise RuntimeError(
            f"no PDF import tool is available for {pdf.name}. {_install_hint()}"
        )

    source_pages = pdf_page_count(pdf)
    excluded = set(excluded_pages or ())
    invalid_exclusions = sorted(page for page in excluded if page < 1 or page > source_pages)
    if invalid_exclusions:
        raise ValueError(
            f"excluded_pages outside 1..{source_pages}: {invalid_exclusions}"
        )
    expected_numbers = [page for page in range(1, source_pages + 1) if page not in excluded]
    if not expected_numbers:
        raise ValueError("excluded_pages removes every PDF page")

    _cleanup_artifacts(out_dir)
    direct = _try_direct_scan_import(pdf, out_dir, source_pages, dpi)
    if direct is not None:
        for page in excluded:
            for artifact in out_dir.glob(f"{_RENDER_PREFIX}-{page:03d}.*"):
                artifact.unlink(missing_ok=True)
        selected: list[Path] = []
        for page in expected_numbers:
            matches = [
                path
                for path in out_dir.glob(f"{_RENDER_PREFIX}-{page:03d}.*")
                if path.suffix.lower() in {".jpg", ".jpeg", ".png"}
            ]
            if len(matches) != 1:
                _cleanup_artifacts(out_dir)
                raise RuntimeError(
                    f"direct PDF import lost or duplicated source page {page}"
                )
            selected.append(matches[0])
        return selected
    _cleanup_artifacts(out_dir)

    errors: list[str] = []
    for name in backends:
        _cleanup_artifacts(out_dir)
        try:
            if not _RENDERERS[name](pdf, out_dir, dpi):
                errors.append(f"{name}: no images produced")
                continue
            pages = _rendered_jpgs(out_dir)
            if len(pages) != source_pages:
                errors.append(
                    f"{name}: page-count mismatch, expected {source_pages}, got {len(pages)}"
                )
                continue
            pages = _normalise_source_page_names(pages, out_dir)
            for page in excluded:
                pages[page - 1].unlink(missing_ok=True)
            return [out_dir / f"{_RENDER_PREFIX}-{page:03d}.jpg" for page in expected_numbers]
        except (OSError, subprocess.SubprocessError) as exc:
            errors.append(f"{name}: {exc}")

    _cleanup_artifacts(out_dir)
    tried = ", ".join(backends) if backends else "direct extraction"
    raise RuntimeError(
        f"PDF import failed for {pdf.name} (tried {tried}); "
        f"page conservation required {source_pages} source pages: {'; '.join(errors)}. "
        f"{_install_hint()}"
    )
