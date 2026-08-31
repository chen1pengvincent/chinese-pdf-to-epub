"""对最终 EPUB 做页守恒、原图、导航和外部资源审计。"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import epub_verify, manifest
from .security_scan import known_secret_values, scan_bytes
from .subprocess_env import safe_subprocess_env

_ANCHOR = re.compile(r'id=["\']source-page-(\d+)["\']')
_CSS_RESOURCE = re.compile(
    r"(?i)(?:url\s*\(\s*['\"]?([^)'\"\s]+)|@import\s+(?:url\s*\()?\s*['\"]?([^)'\"\s;]+))"
)
_ACTIVE_XHTML_TAGS = frozenset({"script", "iframe", "object", "embed"})
_RESOURCE_ATTRIBUTES = frozenset({"href", "src", "poster", "data"})
_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"


def _spine_documents(archive: zipfile.ZipFile) -> list[tuple[str, ET.Element]]:
    container = ET.fromstring(archive.read("META-INF/container.xml"))
    rootfile = container.find(f".//{{{_CONTAINER_NS}}}rootfile")
    if rootfile is None or not rootfile.get("full-path"):
        raise RuntimeError("EPUB container 缺少 rootfile")
    opf_path = str(rootfile.get("full-path"))
    package = ET.fromstring(archive.read(opf_path))
    manifest_node = package.find(f"{{{_OPF_NS}}}manifest")
    spine_node = package.find(f"{{{_OPF_NS}}}spine")
    if manifest_node is None or spine_node is None:
        raise RuntimeError("EPUB package 缺少 manifest 或 spine")
    opf_dir = posixpath.dirname(opf_path)
    by_id = {
        str(item.get("id")): posixpath.normpath(
            posixpath.join(opf_dir, str(item.get("href")))
        )
        for item in manifest_node.findall(f"{{{_OPF_NS}}}item")
        if item.get("id") and item.get("href")
    }
    documents: list[tuple[str, ET.Element]] = []
    for itemref in spine_node.findall(f"{{{_OPF_NS}}}itemref"):
        member = by_id.get(str(itemref.get("idref")))
        if member:
            documents.append((member, ET.fromstring(archive.read(member))))
    return documents


def _anchor_has_local_content(root: ET.Element, page: int) -> bool:
    elements = list(root.iter())
    parents = {
        child: parent
        for parent in elements
        for child in list(parent)
    }
    wanted = f"source-page-{page}"
    for index, element in enumerate(elements):
        if element.get("id") != wanted:
            continue
        local_tag = element.tag.rsplit("}", 1)[-1]
        if local_tag == "h1" and "".join(element.itertext()).strip():
            return True
        parent = parents.get(element)
        if (
            parent is not None
            and _local_name(parent.tag) == "h1"
            and "".join(parent.itertext()).strip()
        ):
            return True
        if "source-page-heading-anchor" in (element.get("class") or "").split():
            # The builder places this marker immediately after a leading H1 so the
            # page heading counts as local content without being copied into the
            # generated navigation document. Stop at any earlier source-page marker
            # to avoid borrowing a heading from a previous page.
            for earlier in reversed(elements[:index]):
                earlier_id = earlier.get("id") or ""
                if earlier_id.startswith("source-page-"):
                    break
                if (
                    _local_name(earlier.tag) == "h1"
                    and "".join(earlier.itertext()).strip()
                ):
                    return True
        for later in elements[index + 1 :]:
            later_id = later.get("id") or ""
            if later_id.startswith("source-page-"):
                return False
            later_tag = later.tag.rsplit("}", 1)[-1]
            if later_tag in {"img", "table", "figure"} or "".join(later.itertext()).strip():
                return True
        return False
    return False


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _safe_file_sha256(path: Path) -> str | None:
    try:
        return manifest.sha256_file(path)
    except OSError:
        return None


def _local_name(name: object) -> str:
    """Return an XML tag/attribute local name with case-insensitive semantics."""
    return str(name).rsplit("}", 1)[-1].casefold()


def _is_external_reference(reference: str) -> bool:
    parsed = urlsplit(reference)
    return bool(parsed.scheme or parsed.netloc or reference.startswith("/"))


def _audit_css_text(
    label: str,
    text: str,
    *,
    errors: list[str],
    external_resources: list[str],
) -> None:
    for match in _CSS_RESOURCE.finditer(text):
        resource = next((value for value in match.groups() if value), "")
        parsed = urlsplit(resource)
        if parsed.scheme not in {"", "data"} or parsed.netloc or resource.startswith("/"):
            external_resources.append(f"{label}: css:{resource}")
    if re.search(r"(?i)(?:expression\s*\(|javascript\s*:)", text):
        errors.append(f"{label} 含危险 CSS/URI 表达式")


def _audit_xhtml_tree(
    label: str,
    root: ET.Element,
    *,
    errors: list[str],
    external_resources: list[str],
) -> None:
    active_tag = False
    event_attribute = False
    style_attribute = False
    css_contexts: list[str] = []
    for element in root.iter():
        tag_name = _local_name(element.tag)
        if tag_name in _ACTIVE_XHTML_TAGS:
            active_tag = True
        if tag_name == "style":
            css_contexts.append("".join(element.itertext()))
        for raw_name, raw_value in element.attrib.items():
            attribute_name = _local_name(raw_name)
            value = str(raw_value).strip()
            if attribute_name.startswith("on"):
                event_attribute = True
            if attribute_name == "style":
                style_attribute = True
                css_contexts.append(value)
            if (
                attribute_name in _RESOURCE_ATTRIBUTES
                and value
                and _is_external_reference(value)
            ):
                external_resources.append(f"{label}: {value}")
    if active_tag:
        errors.append(f"{label} 含不允许的活性元素")
    if event_attribute:
        errors.append(f"{label} 含不允许的事件处理属性")
    if style_attribute:
        errors.append(f"{label} 含不允许的内联 style 属性")
    for css_text in css_contexts:
        _audit_css_text(
            label,
            css_text,
            errors=errors,
            external_resources=external_resources,
        )


def run_epubcheck(epub: Path, executable: str | None = None) -> dict:
    command = executable or shutil.which("epubcheck")
    if not command:
        return {"available": False, "passed": False, "command": None}
    result = subprocess.run(
        [command, "--failonwarnings", str(epub)],
        capture_output=True,
        text=True,
        check=False,
        env=safe_subprocess_env(),
    )
    return {
        "available": True,
        "passed": result.returncode == 0,
        "returncode": result.returncode,
        "output": (result.stdout + "\n" + result.stderr).strip()[-4000:],
    }


def audit_epub(
    book_dir: Path,
    epub: Path,
    *,
    max_xhtml_bytes: int = 262_144,
    require_epubcheck: bool = False,
    epubcheck_executable: str | None = None,
    expected_pages: set[int] | None = None,
    allowed_cover_sha256: str | None = None,
) -> dict:
    data = manifest.load(book_dir)
    all_pages = {item["source_page"] for item in data["pages"]}
    expected_pages = set(expected_pages) if expected_pages is not None else all_pages
    if not expected_pages or not expected_pages <= all_pages:
        raise ValueError("EPUB 审计页集合必须是页清单的非空子集")
    expected_order = [
        page for page in data["source_pdf"]["included_pages"] if page in expected_pages
    ]
    expected_image_by_page = {
        item["source_page"]: item["archive_sha256"]
        for item in data["pages"]
        if item["source_page"] in expected_pages
        and item["representation"] in {"hybrid", "image"}
    }
    expected_image_hashes = set(expected_image_by_page.values())
    allowed_media_hashes = set(expected_image_hashes)
    if allowed_cover_sha256 is not None:
        allowed_media_hashes.add(allowed_cover_sha256)
    structural = epub_verify.validate_epub(epub)
    errors = list(structural.get("errors", []))
    anchors: list[int] = []
    media_hashes: set[str] = set()
    largest_xhtml = 0
    external_resources: list[str] = []
    secret_hits: list[dict[str, str]] = []
    anchor_documents: dict[int, str] = {}
    document_media_hashes: dict[str, set[str]] = {}
    anchor_without_content: list[int] = []
    anchors_in_spine: list[int] = []
    known_values = known_secret_values()
    if not zipfile.is_zipfile(epub):
        checker = run_epubcheck(epub, epubcheck_executable)
        return {
            "schema_version": 1,
            "epub": epub.name,
            "epub_sha256": _safe_file_sha256(epub),
            "valid": False,
            "errors": errors or ["EPUB 不是可读 ZIP"],
            "source_pages": len(expected_pages),
            "source_page_anchors": 0,
            "preserved_image_hashes_expected": len(expected_image_hashes),
            "preserved_image_hashes_found": 0,
            "extra_media_hashes": 0,
            "misplaced_image_pages": [],
            "anchors_in_spine": [],
            "largest_xhtml_bytes": 0,
            "external_resources": [],
            "security_findings": [],
            "epubcheck": checker,
        }
    with zipfile.ZipFile(epub) as archive:
        try:
            spine_documents = _spine_documents(archive)
        except (ET.ParseError, KeyError, RuntimeError, zipfile.BadZipFile) as exc:
            spine_documents = []
            errors.append(f"无法解析 EPUB spine: {exc}")
        for member, root in spine_documents:
            raw = archive.read(member)
            text = raw.decode("utf-8", errors="replace")
            document_anchors = [int(value) for value in _ANCHOR.findall(text)]
            anchors_in_spine.extend(document_anchors)
            for page in document_anchors:
                anchor_documents[page] = member
                if not _anchor_has_local_content(root, page):
                    anchor_without_content.append(page)
                if "title_page" in member.lower() or "title-page" in member.lower():
                    errors.append(f"源页锚点 {page} 错误地落在 EPUB title page")
            hashes: set[str] = set()
            for element in root.iter():
                resource = (element.get("src") or "").strip()
                parsed = urlsplit(resource)
                if not resource or parsed.scheme or parsed.netloc or parsed.path.startswith("/"):
                    continue
                target = posixpath.normpath(
                    posixpath.join(posixpath.dirname(member), unquote(parsed.path))
                )
                if target in archive.namelist():
                    hashes.add(_sha256(archive.read(target)))
            document_media_hashes[member] = hashes
        for info in archive.infolist():
            if info.is_dir():
                continue
            raw = archive.read(info)
            if info.filename.lower().endswith((".xhtml", ".html", ".htm")):
                largest_xhtml = max(largest_xhtml, len(raw))
                text = raw.decode("utf-8", errors="replace")
                anchors.extend(int(value) for value in _ANCHOR.findall(text))
                try:
                    root = ET.fromstring(raw)
                except ET.ParseError:
                    # The structural validator already reports malformed XML.
                    root = None
                if root is not None:
                    _audit_xhtml_tree(
                        info.filename,
                        root,
                        errors=errors,
                        external_resources=external_resources,
                    )
            if info.filename.lower().endswith(".css"):
                text = raw.decode("utf-8", errors="replace")
                _audit_css_text(
                    info.filename,
                    text,
                    errors=errors,
                    external_resources=external_resources,
                )
            if info.filename.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".webp")):
                media_hashes.add(_sha256(raw))
            for finding in scan_bytes(
                info.filename, raw, known_values=known_values
            ):
                secret_hits.append({"path": finding.path, "rule": finding.rule})

    if anchors_in_spine != expected_order:
        errors.append(
            "EPUB spine 中的源页锚点顺序不守恒: "
            f"expected={expected_order[:20]}, actual={anchors_in_spine[:20]}"
        )
    if set(anchors) != expected_pages or len(anchors) != len(expected_pages):
        errors.append(
            "EPUB 源页锚点不守恒: "
            f"expected={len(expected_pages)}, unique={len(set(anchors))}, total={len(anchors)}"
        )
    missing_hashes = sorted(expected_image_hashes - media_hashes)
    if missing_hashes:
        errors.append(f"EPUB 缺少 {len(missing_hashes)} 个应保留的源图哈希")
    extra_media = sorted(media_hashes - allowed_media_hashes)
    if extra_media:
        errors.append(f"EPUB 含 {len(extra_media)} 个未获准的额外栅格资源")
    misplaced_images = [
        page
        for page in expected_image_by_page
        if expected_image_by_page[page]
        not in document_media_hashes.get(anchor_documents.get(page, ""), set())
    ]
    if misplaced_images:
        errors.append(f"原图与对应源页锚点不在同一 spine 文档: {misplaced_images[:20]}")
    if anchor_without_content:
        errors.append(f"源页锚点后没有同文档内容: {sorted(set(anchor_without_content))[:20]}")
    if largest_xhtml > max_xhtml_bytes:
        errors.append(
            f"最大 XHTML 为 {largest_xhtml} bytes，超过门槛 {max_xhtml_bytes}"
        )
    if external_resources:
        errors.append(f"EPUB 含 {len(external_resources)} 个外部或绝对资源引用")
    if secret_hits:
        errors.append(f"EPUB 敏感信息扫描命中 {len(secret_hits)} 项")

    checker = run_epubcheck(epub, epubcheck_executable)
    if require_epubcheck and not checker["available"]:
        errors.append("要求 EPUBCheck，但当前未找到 epubcheck 可执行文件")
    elif checker["available"] and not checker["passed"]:
        errors.append("EPUBCheck --failonwarnings 未通过")

    report = {
        "schema_version": 1,
        "epub": epub.name,
        "epub_sha256": manifest.sha256_file(epub),
        "valid": not errors,
        "errors": errors,
        "source_pages": len(expected_pages),
        "source_page_anchors": len(anchors),
        "preserved_image_hashes_expected": len(expected_image_hashes),
        "preserved_image_hashes_found": len(expected_image_hashes & media_hashes),
        "extra_media_hashes": len(extra_media),
        "misplaced_image_pages": misplaced_images,
        "anchors_in_spine": anchors_in_spine,
        "largest_xhtml_bytes": largest_xhtml,
        "external_resources": external_resources,
        "security_findings": secret_hits,
        "epubcheck": checker,
    }
    return report


def write_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
