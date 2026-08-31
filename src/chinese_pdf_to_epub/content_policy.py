"""Machine-readable content-preservation markers shared by OCR and EPUB build.

The OCR text remains ordinary Markdown.  A marker on its own line asks the
post-processing stage to embed the corresponding source page image as the
authoritative visual representation for charts, diagrams, equations, or tables
whose meaning cannot be preserved by reflowable text alone.
"""

from __future__ import annotations

PRESERVE_PAGE_IMAGE_MARKER = "[[SCAN2EBOOK_PRESERVE_PAGE_IMAGE]]"


def requests_page_image(text: str) -> bool:
    """Return whether OCR output explicitly requests visual preservation."""
    return any(line.strip() == PRESERVE_PAGE_IMAGE_MARKER for line in text.splitlines())


def strip_page_image_marker(text: str) -> str:
    """Remove only exact, standalone preservation markers from OCR Markdown."""
    return "\n".join(
        line for line in text.splitlines()
        if line.strip() != PRESERVE_PAGE_IMAGE_MARKER
    )
