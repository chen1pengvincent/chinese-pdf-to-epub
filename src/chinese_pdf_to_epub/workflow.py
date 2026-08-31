"""中文扫描 PDF 到混合 EPUB 的唯一主流程。"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import (
    artifact_audit,
    cache_provenance,
    content_policy,
    context_prepass,
    corrections,
    epub_build,
    image_ops,
    manifest,
    navigation,
    ocr,
    pdf_render,
    request_control,
    sanitize,
    source_audit,
)

IMAGE_PATTERN = "*.jpg,*.jpeg,*.png"
CONTEXT_BINDING_KEYS = (
    "_source_scan_sha256",
    "_context_model",
    "_context_prompt_sha256",
)


@contextlib.contextmanager
def book_lock(book_dir: Path) -> Iterator[None]:
    """阻止两个进程同时修改同一本书。"""
    lock_path = book_dir / "work/.book.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(f"另一个进程正在处理该书: {book_dir}") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def parse_page_ranges(spec: str | None) -> set[int]:
    if not spec:
        return set()
    values: set[int] = set()
    for token in (part.strip() for part in spec.split(",")):
        if not token:
            continue
        match = re.fullmatch(r"(\d+)(?:-(\d+))?", token)
        if not match:
            raise ValueError(f"无效页码范围: {token!r}")
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < start:
            raise ValueError(f"无效正序页码范围: {token!r}")
        values.update(range(start, end + 1))
    return values


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _source_number(path: Path) -> int:
    numbers = re.findall(r"\d+", path.stem)
    if not numbers:
        raise RuntimeError(f"PDF 渲染器没有保留源页号: {path.name}")
    return int(numbers[-1])


def initialize_book(
    pdf: Path,
    book_dir: Path,
    *,
    excluded_pages: set[int] | None = None,
    exclusion_reason: str | None = None,
    dpi: int = pdf_render.DEFAULT_DPI,
    ocr_max_dimension: int = 0,
) -> dict[str, Any]:
    """导入 PDF 并创建显式页清单；不保存源 PDF 的绝对路径。"""
    pdf = pdf.resolve()
    book_dir = book_dir.resolve()
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        raise FileNotFoundError(f"输入不是可读 PDF: {pdf}")
    excluded = set(excluded_pages or ())
    if excluded and not (exclusion_reason or "").strip():
        raise ValueError("使用 --exclude-pages 时必须同时提供非空 --exclude-reason")
    if dpi < 72 or dpi > 1200:
        raise ValueError("dpi 必须在 72..1200")
    if ocr_max_dimension < 0:
        raise ValueError("ocr_max_dimension 必须大于等于 0")

    source_sha256 = manifest.sha256_file(pdf)
    requested_exclusions = [
        {"page": page, "reason": (exclusion_reason or "").strip()}
        for page in sorted(excluded)
    ]
    manifest_path = book_dir / manifest.MANIFEST_RELATIVE_PATH
    if manifest_path.is_file():
        existing = manifest.load(book_dir)
        source = existing["source_pdf"]
        render = source.get("render", {})
        requested_contract = {
            "filename": pdf.name,
            "sha256": source_sha256,
            "excluded_pages": requested_exclusions,
            "dpi": dpi,
            "ocr_max_dimension": ocr_max_dimension,
        }
        existing_contract = {
            "filename": source.get("filename"),
            "sha256": source.get("sha256"),
            "excluded_pages": source.get("excluded_pages"),
            "dpi": render.get("dpi"),
            "ocr_max_dimension": render.get("ocr_max_dimension"),
        }
        if existing_contract != requested_contract:
            raise RuntimeError(
                "目标目录已有另一份 PDF 或不同导入参数的页清单；拒绝把旧书当成新书"
            )
        return existing

    scans_root = book_dir / "scans"
    if scans_root.exists():
        raise RuntimeError("目标目录已有 scans，但没有有效页清单；请换用空目录人工核对")
    book_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".zhpdf-init-", dir=book_dir) as temporary:
        stage_scans = Path(temporary) / "scans"
        archive_dir = stage_scans / "archive"
        ocr_dir = stage_scans / "ocr"
        archive_dir.mkdir(parents=True)
        ocr_dir.mkdir(parents=True)

        source_count = pdf_render.pdf_page_count(pdf)
        rendered = pdf_render.render_pdf_to_images(
            pdf, archive_dir, dpi=dpi, excluded_pages=excluded
        )
        pages: list[dict[str, Any]] = []
        for path in rendered:
            number = _source_number(path)
            suffix = ".jpg" if path.suffix.lower() == ".jpeg" else path.suffix.lower()
            archive = archive_dir / f"page_{number:06d}{suffix}"
            path.rename(archive)
            ocr_image = ocr_dir / f"page_{number:06d}.jpg"
            optimized = False
            if ocr_max_dimension > 0:
                optimized = image_ops.downscale_to_jpeg(archive, ocr_image, ocr_max_dimension)
            if not optimized:
                ocr_image = ocr_dir / archive.name
                shutil.copy2(archive, ocr_image)
            pages.append(
                {
                    "source_page": number,
                    "archive_image": f"scans/archive/{archive.name}",
                    "archive_sha256": manifest.sha256_file(archive),
                    "ocr_image": f"scans/ocr/{ocr_image.name}",
                    "ocr_sha256": manifest.sha256_file(ocr_image),
                    "render_mode": "pdf-direct-or-rendered",
                    "ocr_state": "pending",
                    "attempts": 0,
                    "representation": "pending",
                    "text_sha256": None,
                    "final_text_sha256": None,
                    "fallback_reason": None,
                }
            )
        pages.sort(key=lambda item: item["source_page"])
        included = [number for number in range(1, source_count + 1) if number not in excluded]
        if [item["source_page"] for item in pages] != included:
            raise RuntimeError("PDF 导入后的源页集合不守恒")
        if manifest.sha256_file(pdf) != source_sha256:
            raise RuntimeError("PDF 在页数核验或渲染期间发生变化；拒绝发布不一致页清单")

        profile = {
            "schema_version": 1,
            "mode": "pdf-import",
            "dpi": dpi,
            "ocr_max_dimension": ocr_max_dimension,
            "page_count": len(pages),
            "source_page_numbers": included,
            "excluded_pdf_pages": sorted(excluded),
        }
        _atomic_json(ocr_dir / cache_provenance.SOURCE_MANIFEST_NAME, profile)
        os.replace(stage_scans, scans_root)

    data: dict[str, Any] = {
        "schema_version": manifest.SCHEMA_VERSION,
        "project": "chinese-pdf-to-epub",
        "source_pdf": {
            "filename": pdf.name,
            "sha256": source_sha256,
            "page_count": source_count,
            "included_pages": included,
            "excluded_pages": requested_exclusions,
            "render": profile,
        },
        "pages": pages,
    }
    try:
        manifest.save(book_dir, data)
    except Exception:
        shutil.rmtree(scans_root, ignore_errors=True)
        raise
    source_audit.write_report(
        book_dir / "work/audit/source-audit.json", source_audit.audit(book_dir)
    )
    return data


def _ordered_ocr_images(book_dir: Path, data: dict[str, Any]) -> list[Path]:
    return [manifest.project_path(book_dir, item["ocr_image"]) for item in data["pages"]]


def _context_binding(book_dir: Path, data: dict[str, Any]) -> dict[str, str]:
    pages = _ordered_ocr_images(book_dir, data)
    return {
        "_source_scan_sha256": cache_provenance.scan_set_sha256(pages),
        "_context_model": ocr.DEFAULT_MODEL,
        "_context_prompt_sha256": hashlib.sha256(
            context_prepass.context_prompt_for_lang("zh").encode("utf-8")
        ).hexdigest(),
    }


def _verified_context_data(book_dir: Path, data: dict[str, Any]) -> dict[str, Any]:
    context_path = book_dir / "work/context.json"
    if context_path.is_symlink():
        raise RuntimeError("context.json 不能是符号链接")
    try:
        cached = json.loads(context_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"context.json 不可用: {exc}") from exc
    expected = _context_binding(book_dir, data)
    if not isinstance(cached, dict) or any(cached.get(key) != value for key, value in expected.items()):
        raise RuntimeError("context 与当前页图、模型或提示词不匹配；拒绝跨书复用")
    context_prepass._validate_zh_context(cached, source=str(context_path))
    return cached


def _verified_context_block(book_dir: Path, data: dict[str, Any]) -> str:
    return context_prepass.render_block(_verified_context_data(book_dir, data))


def cover_for_book(
    book_dir: Path, data: dict[str, Any] | None = None
) -> tuple[Path | None, int | None]:
    data = data or manifest.load(book_dir)
    context = _verified_context_data(book_dir, data)
    cover_name = context.get("cover_page")
    if cover_name is None:
        return None, None
    matches = [
        item
        for item in data["pages"]
        if Path(item["ocr_image"]).name == cover_name
    ]
    if len(matches) != 1:
        raise RuntimeError(f"context.cover_page 无法唯一映射到纳入页: {cover_name!r}")
    item = matches[0]
    return manifest.project_path(book_dir, item["archive_image"]), item["source_page"]


def ensure_context(
    book_dir: Path,
    api_key: str,
    *,
    retries: int = 2,
    timeout_s: float = 300,
    ledger: request_control.RequestLedger | None = None,
) -> str:
    data = manifest.load(book_dir)
    work_dir = book_dir / "work"
    expected = _context_binding(book_dir, data)
    context_path = work_dir / "context.json"
    if context_path.is_file():
        return _verified_context_block(book_dir, data)

    result = context_prepass.run_prepass(
        api_key,
        ocr.DEFAULT_MODEL,
        book_dir / "scans/ocr",
        IMAGE_PATTERN,
        out_dir=work_dir,
        lang="zh",
        retries=retries,
        request_options=request_control.RequestOptions(timeout_s=timeout_s),
        ledger=ledger,
        persist=False,
    )
    context = dict(result["context"])
    context.update(expected)
    block = context_prepass.render_block(context)
    context_prepass.save_context(work_dir, context, block)
    return block


def _count_attempts(ledger_path: Path) -> dict[int, int]:
    counts: dict[int, int] = {}
    if not ledger_path.is_file():
        return counts
    if ledger_path.is_symlink():
        raise RuntimeError("请求台账不能是符号链接")
    for line_number, line in enumerate(
        ledger_path.read_text(encoding="utf-8").splitlines(), 1
    ):
        try:
            record = json.loads(line)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"请求台账第 {line_number} 行不是有效 JSON") from exc
        if not isinstance(record, dict) or record.get("event") != "started" or record.get("stage") != "ocr":
            continue
        match = re.search(r"(\d+)", str(record.get("page", "")))
        if not match:
            raise RuntimeError(f"请求台账第 {line_number} 行的 OCR 页标识无效")
        page = int(match.group(1))
        counts[page] = counts.get(page, 0) + 1
    return counts


def _failure_class(message: str) -> tuple[str, str]:
    lowered = message.lower()
    if "http 401" in lowered or "http 403" in lowered or "http 404" in lowered:
        return "systemic", "认证、权限或端点配置错误"
    if "http 402" in lowered:
        return "systemic", "服务额度不足"
    if "timeout" in lowered or "billing are unknown" in lowered or "network failure" in lowered:
        return "ambiguous", "请求结果或计费状态不确定"
    if "unsupported chinese layout" in lowered:
        return "unsupported", "页面版式超出首版支持范围"
    if "http 429" in lowered or "http 5" in lowered:
        return "failed", "瞬时服务错误在重试上限后仍失败"
    return "failed", "OCR 请求失败"


def _refresh_states(
    book_dir: Path,
    data: dict[str, Any],
    summary: dict[str, Any],
    ledger_path: Path,
) -> tuple[dict[str, Any], bool]:
    failure_by_page: dict[int, str] = {}
    for filename, error in summary.get("failures", []):
        match = re.search(r"(\d+)", filename)
        if match:
            failure_by_page[int(match.group(1))] = error
    attempts = _count_attempts(ledger_path)
    systemic = False
    raw_dir = book_dir / "work/ocr/raw"
    for item in data["pages"]:
        page = item["source_page"]
        item["attempts"] = max(attempts.get(page, 0), item.get("attempts", 0))
        markdown = raw_dir / f"page_{page:06d}.md"
        if markdown.is_file() and markdown.stat().st_size > 0:
            text = markdown.read_text(encoding="utf-8").strip()
            item["text_sha256"] = manifest.sha256_file(markdown)
            if text == ocr.BLANK_PLACEHOLDER:
                item.update(ocr_state="blank", representation="image", fallback_reason="blank-page")
            elif text.startswith(ocr.DEAD_PREFIX):
                error = failure_by_page.get(page, "deterministic fallback")
                state, reason = _failure_class(error)
                item.update(
                    ocr_state="unsupported" if state == "unsupported" else "failed",
                    representation="image",
                    fallback_reason=reason,
                )
            elif content_policy.requests_page_image(text):
                item.update(ocr_state="ok", representation="hybrid", fallback_reason=None)
            else:
                item.update(ocr_state="ok", representation="reflow", fallback_reason=None)
        elif page in failure_by_page:
            state, reason = _failure_class(failure_by_page[page])
            systemic = systemic or state == "systemic"
            if state == "systemic":
                item.update(ocr_state="pending", representation="pending", fallback_reason=reason)
            else:
                item.update(ocr_state=state, representation="image", fallback_reason=reason)
    manifest.save(book_dir, data)
    return data, systemic


def _reconcile_checkpointed_raw(
    book_dir: Path,
    data: dict[str, Any],
    ledger_path: Path,
) -> dict[str, Any]:
    """Promote provenance-verified OCR checkpoints before applying retry caps.

    A process can stop after a paid result is checkpointed but before the page
    manifest is refreshed.  In particular, a sixth successful attempt must not
    be converted to an image fallback merely because its manifest row is still
    ``pending``.  Only a cache bound to this book/context is reconciled.
    """
    raw_dir = book_dir / "work/ocr/raw"
    if not raw_dir.is_dir():
        return data
    has_checkpoint = any(
        path.name.endswith(".pending")
        or (
            (path.is_file() or path.is_symlink())
            and path.name.startswith("page_")
            and path.suffix == ".md"
        )
        for path in raw_dir.iterdir()
    )
    if not has_checkpoint:
        return data

    context = _verified_context_block(book_dir, data)
    pages = _ordered_ocr_images(book_dir, data)
    config = cache_provenance.build_config(
        input_dir=book_dir / "scans/ocr",
        model=ocr.DEFAULT_MODEL,
        lang="zh",
        prompt_text=ocr.prompt_for_lang("zh"),
        prompt_context=context,
    )
    cache_provenance.prepare_cache(
        input_dir=book_dir / "scans/ocr",
        output_dir=raw_dir,
        pages=pages,
        config=config,
    )
    reconciled, _ = _refresh_states(book_dir, data, {"failures": []}, ledger_path)
    return reconciled


def run_ocr(
    book_dir: Path,
    api_key: str,
    *,
    workers: int = 4,
    retries: int = 5,
    max_tokens: int = 12_000,
    timeout_s: float = 300,
    selected_pages: set[int] | None = None,
    on_event=None,
) -> dict[str, Any]:
    if retries < 0 or retries > 5:
        raise ValueError("retries 必须在 0..5；首次请求加 5 次恢复尝试即上限 6 次")
    data = manifest.load(book_dir)
    ledger_path = book_dir / "work/request-ledger.jsonl"
    ledger_attempts = _count_attempts(ledger_path)
    regressed = {
        item["source_page"]: {
            "manifest": item.get("attempts", 0),
            "ledger": ledger_attempts.get(item["source_page"], 0),
        }
        for item in data["pages"]
        if item.get("attempts", 0) > ledger_attempts.get(item["source_page"], 0)
    }
    if regressed:
        raise RuntimeError(f"请求台账少于页清单已记录次数；拒绝放宽绝对上限: {regressed}")
    data = _reconcile_checkpointed_raw(book_dir, data, ledger_path)
    prior_attempts = {
        item["source_page"]: max(
            ledger_attempts.get(item["source_page"], 0), item.get("attempts", 0)
        )
        for item in data["pages"]
    }
    all_pages = {item["source_page"] for item in data["pages"]}
    selected = set(selected_pages) if selected_pages is not None else all_pages
    missing = selected - all_pages
    if missing:
        raise ValueError(f"选择页不在纳入范围: {sorted(missing)}")
    exceeded = {page: prior_attempts[page] for page in all_pages if prior_attempts.get(page, 0) > 6}
    if exceeded:
        raise RuntimeError(f"请求台账显示页请求次数已超过绝对上限 6: {exceeded}")
    eligible: set[int] = set()
    for item in data["pages"]:
        page = item["source_page"]
        item["attempts"] = prior_attempts.get(page, item.get("attempts", 0))
        if page not in selected or item["ocr_state"] != "pending":
            continue
        if item["attempts"] >= 6:
            item.update(
                ocr_state="failed",
                representation="image",
                fallback_reason="已达到每页最多 6 次请求上限",
            )
        else:
            eligible.add(page)
    manifest.save(book_dir, data)
    if not eligible:
        return {
            "ok": 0,
            "fail": 0,
            "blank": 0,
            "skipped": len(selected),
            "total": len(selected),
            "cost_usd": 0.0,
            "cost_status": "known",
            "billing_unknown_requests": 0,
            "failures": [],
        }

    ledger = request_control.RequestLedger(ledger_path)
    context = ensure_context(book_dir, api_key, timeout_s=timeout_s, ledger=ledger)
    pages = _ordered_ocr_images(book_dir, data)
    raw_dir = book_dir / "work/ocr/raw"
    config = cache_provenance.build_config(
        input_dir=book_dir / "scans/ocr",
        model=ocr.DEFAULT_MODEL,
        lang="zh",
        prompt_text=ocr.prompt_for_lang("zh"),
        prompt_context=context,
    )
    cache_provenance.prepare_cache(
        input_dir=book_dir / "scans/ocr",
        output_dir=raw_dir,
        pages=pages,
        config=config,
    )

    temporary = tempfile.TemporaryDirectory(prefix="zhpdf-ocr-", dir=book_dir / "work")
    input_dir = Path(temporary.name)
    by_number = {item["source_page"]: item for item in data["pages"]}
    retries_by_page: dict[str, int] = {}
    for number in sorted(eligible):
        source = manifest.project_path(book_dir, by_number[number]["ocr_image"])
        (input_dir / source.name).symlink_to(source)
        retries_by_page[source.name] = min(retries, 5 - prior_attempts.get(number, 0))
    try:
        summary = ocr.run_batch(
            api_key=api_key,
            input_dir=input_dir,
            output_dir=raw_dir,
            model=ocr.DEFAULT_MODEL,
            workers=workers,
            pattern=IMAGE_PATTERN,
            max_tokens=max_tokens,
            retries=retries,
            on_event=on_event,
            prompt_context=context,
            lang="zh",
            request_options=request_control.RequestOptions(timeout_s=timeout_s),
            ledger=ledger,
            retries_by_page=retries_by_page,
            checkpoint_page=lambda page, text: cache_provenance.checkpoint_page(
                output_dir=raw_dir,
                page=page,
                markdown_text=text,
                config=config,
            ),
        )
    finally:
        temporary.cleanup()
    cache_provenance.finalize_cache(output_dir=raw_dir, pages=pages, config=config)
    data, systemic = _refresh_states(book_dir, data, summary, ledger_path)
    if systemic:
        raise RuntimeError("检测到全局认证、端点或额度错误；已停止，未把未请求页伪装成失败页")
    return summary


def representative_pages(data: dict[str, Any], count: int = 7) -> set[int]:
    pages = data["source_pdf"]["included_pages"]
    count = max(1, min(count, len(pages)))
    if count == 1:
        return {pages[0]}
    return {pages[round((len(pages) - 1) * index / (count - 1))] for index in range(count)}


def _verify_raw_cache(book_dir: Path, data: dict[str, Any]) -> None:
    context = _verified_context_block(book_dir, data)
    pages = _ordered_ocr_images(book_dir, data)
    raw_dir = book_dir / "work/ocr/raw"
    config = cache_provenance.build_config(
        input_dir=book_dir / "scans/ocr",
        model=ocr.DEFAULT_MODEL,
        lang="zh",
        prompt_text=ocr.prompt_for_lang("zh"),
        prompt_context=context,
    )
    cache_provenance.prepare_cache(
        input_dir=book_dir / "scans/ocr",
        output_dir=raw_dir,
        pages=pages,
        config=config,
    )
    for item in data["pages"]:
        raw = raw_dir / f"page_{item['source_page']:06d}.md"
        if raw.is_file():
            expected = item.get("text_sha256")
            if not expected or expected != manifest.sha256_file(raw):
                raise RuntimeError(
                    f"第 {item['source_page']} 页原始 OCR 与页清单哈希不一致"
                )
        elif item["ocr_state"] == "ok":
            raise RuntimeError(f"第 {item['source_page']} 页标记 OCR 成功但原始文本缺失")


def prepare_final_text(
    book_dir: Path, *, force_rederive: bool = False
) -> tuple[dict[int, str], dict[str, Any]]:
    data = manifest.load(book_dir)
    _verify_raw_cache(book_dir, data)
    final_dir = book_dir / "work/ocr/final"
    report = corrections.apply(
        book_dir, final_dir=final_dir, force_rederive=force_rederive
    )
    data = manifest.load(book_dir)
    override_pages = {item["page"] for item in report.get("overrides", [])}
    texts: dict[int, str] = {}
    for item in data["pages"]:
        page = item["source_page"]
        path = final_dir / f"page_{page:06d}.md"
        is_override = page in override_pages
        if item["representation"] in {"reflow", "hybrid"} or is_override:
            text = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
            visible = content_policy.strip_page_image_marker(text).strip()
            invalid = not visible or text == ocr.BLANK_PLACEHOLDER or text.startswith(ocr.DEAD_PREFIX)
            if invalid:
                if is_override:
                    raise RuntimeError(f"第 {page} 页重试覆盖没有可验证的正文，拒绝晋升")
                item.update(
                    ocr_state="failed",
                    representation="image",
                    final_text_sha256=None,
                    fallback_reason="清洗或修订后没有可信文本",
                )
            else:
                if is_override:
                    item.update(
                        ocr_state="ok",
                        representation=(
                            "hybrid" if content_policy.requests_page_image(text) else "reflow"
                        ),
                        fallback_reason=None,
                    )
                texts[page] = visible
                item["final_text_sha256"] = manifest.sha256_file(path)
    manifest.save(book_dir, data)
    return texts, report


def build_book(
    book_dir: Path,
    *,
    title: str,
    author: str | None = None,
    year: str | None = None,
    lang: str = "zh-CN",
    require_epubcheck: bool = False,
    epubcheck_executable: str | None = None,
    max_xhtml_bytes: int = 262_144,
    force_rederive: bool = False,
) -> tuple[Path, dict[str, Any]]:
    data = manifest.load(book_dir)
    pending = [item["source_page"] for item in data["pages"] if item["ocr_state"] == "pending"]
    if pending:
        raise RuntimeError(f"仍有 {len(pending)} 页未形成终态，拒绝构建: {pending[:20]}")
    texts, _ = prepare_final_text(book_dir, force_rederive=force_rederive)
    source_audit.write_report(
        book_dir / "work/audit/source-audit.json",
        source_audit.audit(book_dir, texts),
    )
    navigation_path = book_dir / "work/navigation.json"
    nav = navigation.load_or_generate(navigation_path, texts)
    entries = {item["source_page"]: item for item in nav["entries"]}

    data = manifest.load(book_dir)
    pages: list[epub_build.HybridPage] = []
    for item in data["pages"]:
        page = item["source_page"]
        text = navigation.apply_entry(texts.get(page, ""), entries.get(page))
        if item["representation"] == "reflow":
            page_type, status = "text", "ok"
        elif item["representation"] == "hybrid":
            page_type, status = "complex_table", "ok"
        else:
            page_type = "failed"
            status = "blank" if item["ocr_state"] == "blank" else "failed"
        pages.append(
            epub_build.HybridPage(
                source_page=page,
                image=manifest.project_path(book_dir, item["archive_image"]),
                page_type=page_type,
                ocr_status=status,
                ocr_text=text or None,
            )
        )

    work_epub = book_dir / "work/book.staged.epub"
    work_epub.unlink(missing_ok=True)
    cover, cover_page = cover_for_book(book_dir, data)
    epub_build.build_hybrid_epub(
        pages=pages,
        output_epub=work_epub,
        title=title,
        author=author,
        lang=lang,
        year=year,
        cover=cover,
        expected_page_numbers=data["source_pdf"]["included_pages"],
    )
    report = artifact_audit.audit_epub(
        book_dir,
        work_epub,
        max_xhtml_bytes=max_xhtml_bytes,
        require_epubcheck=require_epubcheck,
        epubcheck_executable=epubcheck_executable,
        allowed_cover_sha256=(
            next(
                item["archive_sha256"]
                for item in data["pages"]
                if item["source_page"] == cover_page
            )
            if cover_page is not None
            else None
        ),
    )
    artifact_audit.write_report(book_dir / "work/audit/acceptance.json", report)
    if not report["valid"]:
        rejected = book_dir / "work/book.rejected.epub"
        os.replace(work_epub, rejected)
        raise RuntimeError("EPUB 未通过发布门禁: " + "; ".join(report["errors"]))
    output = book_dir / "dist/book.epub"
    output.parent.mkdir(parents=True, exist_ok=True)
    os.replace(work_epub, output)
    artifact_audit.write_report(book_dir / "dist/acceptance.json", report)
    return output, report


def build_smoke_epub(book_dir: Path, selected_pages: set[int], *, title: str) -> Path:
    data = manifest.load(book_dir)
    raw_dir = book_dir / "work/ocr/raw"
    by_number = {item["source_page"]: item for item in data["pages"]}
    pages: list[epub_build.HybridPage] = []
    for number in sorted(selected_pages):
        item = by_number[number]
        raw = raw_dir / f"page_{number:06d}.md"
        text = raw.read_text(encoding="utf-8").strip() if raw.is_file() else ""
        preserve = content_policy.requests_page_image(text)
        if item["representation"] == "pending":
            raise RuntimeError(f"代表页 {number} 没有形成终态")
        status = "ok" if item["representation"] in {"reflow", "hybrid"} else "failed"
        page_type = "complex_table" if preserve else ("text" if status == "ok" else "failed")
        pages.append(
            epub_build.HybridPage(
                number,
                manifest.project_path(book_dir, item["archive_image"]),
                page_type,
                status,
                sanitize.sanitize_ocr_markdown(
                    content_policy.strip_page_image_marker(text)
                )
                if status == "ok"
                else None,
            )
        )
    output = book_dir / "work/smoke.epub"
    epub_build.build_hybrid_epub(
        pages=pages,
        output_epub=output,
        title=f"{title}（代表页测试）",
        lang="zh-CN",
        expected_page_numbers=sorted(selected_pages),
    )
    report = artifact_audit.audit_epub(
        book_dir,
        output,
        expected_pages=set(selected_pages),
    )
    artifact_audit.write_report(book_dir / "work/audit/smoke-acceptance.json", report)
    if not report["valid"]:
        output.unlink(missing_ok=True)
        raise RuntimeError("代表页 EPUB 未通过验收: " + "; ".join(report["errors"]))
    return output
