"""Batch and structural EPUB verification using only the Python standard library.

``zipfile.testzip()`` proves only that bytes can be decompressed. It does not
prove that an EPUB reader can find the package, spine, navigation, cover, or
resources. This module therefore validates the EPUB container and its internal
references before reporting ``OK``.

The CLI status vocabulary remains backward compatible:

* ``OK`` -- size, ZIP integrity, and EPUB structure all pass;
* ``TINY`` -- suspiciously small output;
* ``BADZIP`` -- unreadable ZIP *or* invalid EPUB structure;
* ``MISSING`` -- expected output does not exist.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

TINY_BYTES = 10_240

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_LANG_RE = re.compile(r"^[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*$")


@dataclass
class VerifyResult:
    label: str
    path: Path
    status: str
    size: int = 0
    errors: tuple[str, ...] = field(default_factory=tuple)


def _safe_member(base: str, reference: str) -> str | None:
    """Resolve a local EPUB reference; return None for external/data links."""
    parsed = urlsplit(reference)
    if parsed.scheme or parsed.netloc:
        return None
    raw_path = unquote(parsed.path)
    if not raw_path:
        return posixpath.normpath(base)
    if raw_path.startswith("/"):
        return ""
    resolved = posixpath.normpath(posixpath.join(posixpath.dirname(base), raw_path))
    if resolved == ".." or resolved.startswith("../"):
        return ""
    return resolved


def _tokens(value: str | None) -> set[str]:
    return set((value or "").split())


def _nonempty_texts(parent: ET.Element | None, tag: str) -> list[str]:
    if parent is None:
        return []
    return [
        (element.text or "").strip()
        for element in parent.findall(tag)
        if (element.text or "").strip()
    ]


def validate_epub(path: Path | str) -> dict:
    """Validate EPUB container, package metadata, reading order and references.

    No exception escapes for a malformed artifact. The return mapping contains
    ``valid``, ``errors``, and the discovered title/language/OPF path when present.
    """
    path = Path(path)
    errors: list[str] = []
    facts: dict[str, object] = {"valid": False, "errors": errors}
    if not path.is_file():
        errors.append("EPUB file does not exist")
        return facts

    try:
        with zipfile.ZipFile(path, "r") as zf:
            infos = zf.infolist()
            names = [info.filename for info in infos]
            name_set = set(names)
            if len(name_set) != len(names):
                errors.append("ZIP contains duplicate member names")

            if not infos or infos[0].filename != "mimetype":
                errors.append("mimetype is not the first ZIP entry")
            elif infos[0].compress_type != zipfile.ZIP_STORED:
                errors.append("mimetype must be stored without compression")
            if "mimetype" not in name_set:
                errors.append("mimetype entry is missing")
            elif zf.read("mimetype") != b"application/epub+zip":
                errors.append("mimetype content is not application/epub+zip")

            bad = zf.testzip()
            if bad is not None:
                errors.append(f"CRC failure in ZIP member: {bad}")

            container_name = "META-INF/container.xml"
            if container_name not in name_set:
                errors.append("META-INF/container.xml is missing")
                return {**facts, "valid": False}
            try:
                container = ET.fromstring(zf.read(container_name))
            except ET.ParseError as exc:
                errors.append(f"container.xml is not valid XML: {exc}")
                return {**facts, "valid": False}

            rootfiles = container.findall(f".//{{{_CONTAINER_NS}}}rootfile")
            if not rootfiles:
                errors.append("container.xml has no rootfile")
                return {**facts, "valid": False}
            opf_path = (rootfiles[0].get("full-path") or "").strip()
            opf_media = (rootfiles[0].get("media-type") or "").strip()
            if not opf_path or opf_path.startswith("/") or ".." in opf_path.split("/"):
                errors.append("container rootfile has an unsafe or empty full-path")
                return {**facts, "valid": False}
            if opf_media and opf_media != "application/oebps-package+xml":
                errors.append(f"container rootfile has unexpected media-type: {opf_media}")
            if opf_path not in name_set:
                errors.append(f"package document is missing: {opf_path}")
                return {**facts, "valid": False, "opf_path": opf_path}
            facts["opf_path"] = opf_path

            try:
                package = ET.fromstring(zf.read(opf_path))
            except ET.ParseError as exc:
                errors.append(f"package document is not valid XML: {exc}")
                return {**facts, "valid": False}

            metadata = package.find(f"{{{_OPF_NS}}}metadata")
            manifest = package.find(f"{{{_OPF_NS}}}manifest")
            spine = package.find(f"{{{_OPF_NS}}}spine")
            if metadata is None:
                errors.append("OPF metadata element is missing")
            if manifest is None:
                errors.append("OPF manifest element is missing")
            if spine is None:
                errors.append("OPF spine element is missing")

            titles = _nonempty_texts(metadata, f"{{{_DC_NS}}}title")
            languages = _nonempty_texts(metadata, f"{{{_DC_NS}}}language")
            identifiers = _nonempty_texts(metadata, f"{{{_DC_NS}}}identifier")
            if not titles:
                errors.append("OPF has no non-empty dc:title")
            else:
                facts["title"] = titles[0]
            if not languages:
                errors.append("OPF has no non-empty dc:language")
            else:
                facts["language"] = languages[0]
                if not _LANG_RE.fullmatch(languages[0]):
                    errors.append(f"dc:language is not a plausible BCP 47 tag: {languages[0]!r}")
            if not identifiers:
                errors.append("OPF has no non-empty dc:identifier")

            unique_identifier = (package.get("unique-identifier") or "").strip()
            if unique_identifier:
                matching_id = metadata.find(
                    f"{{{_DC_NS}}}identifier[@id='{unique_identifier}']"
                ) if metadata is not None else None
                if matching_id is None or not (matching_id.text or "").strip():
                    errors.append("package unique-identifier does not resolve")

            manifest_by_id: dict[str, tuple[str, str, set[str]]] = {}
            manifest_paths: dict[str, str] = {}
            nav_items: list[tuple[str, str]] = []
            cover_items: list[tuple[str, str]] = []
            opf_dir_anchor = posixpath.join(posixpath.dirname(opf_path), "_")
            if manifest is not None:
                for item in manifest.findall(f"{{{_OPF_NS}}}item"):
                    item_id = (item.get("id") or "").strip()
                    href = (item.get("href") or "").strip()
                    media_type = (item.get("media-type") or "").strip()
                    props = _tokens(item.get("properties"))
                    if not item_id or not href or not media_type:
                        errors.append("manifest item is missing id, href, or media-type")
                        continue
                    if item_id in manifest_by_id:
                        errors.append(f"duplicate manifest id: {item_id}")
                        continue
                    member = _safe_member(opf_dir_anchor, href)
                    if not member:
                        errors.append(f"manifest href escapes the EPUB root: {href}")
                        continue
                    if member in manifest_paths:
                        errors.append(
                            f"duplicate manifest resource: {href} and {manifest_paths[member]}"
                        )
                    manifest_paths[member] = href
                    manifest_by_id[item_id] = (member, media_type, props)
                    if member not in name_set:
                        errors.append(f"manifest resource is missing: {href}")
                    if "nav" in props:
                        nav_items.append((item_id, member))
                    if "cover-image" in props:
                        cover_items.append((item_id, member))

            if len(nav_items) != 1:
                errors.append(f"manifest must contain exactly one nav item; found {len(nav_items)}")
            if len(cover_items) > 1:
                errors.append("manifest contains more than one cover-image item")
            elif cover_items:
                cover_id = cover_items[0][0]
                cover_media_type = manifest_by_id.get(cover_id, ("", "", set()))[1]
                if not cover_media_type.startswith("image/"):
                    errors.append("cover-image manifest item is not an image resource")

            spine_refs: list[str] = []
            if spine is not None:
                for itemref in spine.findall(f"{{{_OPF_NS}}}itemref"):
                    idref = (itemref.get("idref") or "").strip()
                    spine_refs.append(idref)
                    if idref not in manifest_by_id:
                        errors.append(f"spine idref does not resolve: {idref}")
                    elif manifest_by_id[idref][1] != "application/xhtml+xml":
                        errors.append(f"spine item is not XHTML: {idref}")
                if not spine_refs:
                    errors.append("OPF spine is empty")

            # EPUB 2-style cover metadata is allowed, but it must resolve and be
            # consistent with EPUB 3's cover-image property when both exist.
            cover_meta_ids: list[str] = []
            if metadata is not None:
                for meta in metadata.findall(f"{{{_OPF_NS}}}meta"):
                    if (meta.get("name") or "").strip() == "cover":
                        cover_meta_ids.append((meta.get("content") or "").strip())
            for cover_id in cover_meta_ids:
                if cover_id not in manifest_by_id:
                    errors.append(f"cover metadata does not resolve: {cover_id}")
            if cover_items and cover_meta_ids and cover_items[0][0] not in cover_meta_ids:
                errors.append("EPUB 2 cover metadata and EPUB 3 cover-image disagree")

            guide = package.find(f"{{{_OPF_NS}}}guide")
            if guide is not None:
                for reference in guide.findall(f"{{{_OPF_NS}}}reference"):
                    href = (reference.get("href") or "").strip()
                    if not href:
                        errors.append("OPF guide reference has no href")
                        continue
                    target = _safe_member(opf_dir_anchor, href)
                    if not target:
                        errors.append(f"OPF guide reference escapes EPUB root: {href}")
                    elif target not in name_set:
                        errors.append(f"OPF guide reference is broken: {href}")

            # Parse every XHTML and verify its local href/src/poster/data targets.
            parsed_xhtml: dict[str, ET.Element] = {}
            for member in manifest_paths:
                media_type = next(
                    (value[1] for value in manifest_by_id.values() if value[0] == member),
                    "",
                )
                if media_type != "application/xhtml+xml" or member not in name_set:
                    continue
                try:
                    root = ET.fromstring(zf.read(member))
                except ET.ParseError as exc:
                    errors.append(f"XHTML resource is not valid XML ({member}): {exc}")
                    continue
                parsed_xhtml[member] = root
                for element in root.iter():
                    for attr in ("href", "src", "poster", "data"):
                        reference = (element.get(attr) or "").strip()
                        if not reference or reference.startswith("#"):
                            continue
                        target = _safe_member(member, reference)
                        if target == "":
                            errors.append(f"resource reference escapes EPUB root: {member} -> {reference}")
                        elif target is not None and target not in name_set:
                            errors.append(f"broken resource reference: {member} -> {reference}")

            if len(nav_items) == 1:
                nav_member = nav_items[0][1]
                nav_root = parsed_xhtml.get(nav_member)
                if nav_root is not None:
                    toc_nodes = [
                        node for node in nav_root.findall(f".//{{{_XHTML_NS}}}nav")
                        if "toc" in _tokens(node.get(f"{{{_EPUB_NS}}}type"))
                    ]
                    if not toc_nodes:
                        errors.append("navigation document has no epub:type='toc' nav")

            facts["spine_items"] = len(spine_refs)
            facts["manifest_items"] = len(manifest_by_id)
            facts["has_cover"] = bool(cover_items or cover_meta_ids)
    except (zipfile.BadZipFile, OSError, RuntimeError, NotImplementedError) as exc:
        errors.append(f"cannot read EPUB ZIP: {exc}")

    facts["valid"] = not errors
    return facts


def _check_epub_detailed(path: Path) -> tuple[str, int, tuple[str, ...]]:
    if not path.is_file():
        return "MISSING", 0, ()
    try:
        size = path.stat().st_size
    except OSError as exc:
        return "BADZIP", 0, (f"cannot stat EPUB: {exc}",)
    if size < TINY_BYTES:
        return "TINY", size, ()
    validation = validate_epub(path)
    if not validation["valid"]:
        return "BADZIP", size, tuple(validation["errors"])
    return "OK", size, ()


def _check_epub(path: Path) -> tuple[str, int]:
    """Backward-compatible two-field status helper."""
    status, size, _errors = _check_epub_detailed(path)
    return status, size


def _expected_epub(book_home: Path) -> Path:
    return book_home / "dist" / f"{book_home.name}.epub"


def _is_book_home(path: Path) -> bool:
    return (path / "scans").is_dir() or (path / "dist").is_dir()


def _result(label: str, path: Path) -> VerifyResult:
    status, size, errors = _check_epub_detailed(path)
    return VerifyResult(label, path, status, size, errors)


def verify_paths(paths: list[Path]) -> list[VerifyResult]:
    """Resolve EPUB files/book homes/parent directories and validate all outputs."""
    results: list[VerifyResult] = []
    for raw in paths:
        path = raw.expanduser()
        if path.suffix == ".epub" or path.is_file():
            results.append(_result(path.name, path))
        elif path.is_dir() and _is_book_home(path):
            results.append(_result(path.name, _expected_epub(path)))
        elif path.is_dir():
            homes = sorted(
                (child for child in path.iterdir() if child.is_dir() and _is_book_home(child)),
                key=lambda child: child.name,
            )
            if not homes:
                results.append(VerifyResult(path.name, path, "MISSING", 0))
                continue
            for home in homes:
                results.append(_result(home.name, _expected_epub(home)))
        else:
            results.append(VerifyResult(path.name, path, "MISSING", 0))
    return results


def summarize(results: list[VerifyResult]) -> dict:
    counts = {"OK": 0, "TINY": 0, "BADZIP": 0, "MISSING": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    counts["total"] = len(results)
    return counts
