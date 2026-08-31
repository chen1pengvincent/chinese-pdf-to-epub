"""zhpdf2epub：中文扫描 PDF 到混合 EPUB。"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import artifact_audit, manifest, ocr, source_audit, workflow


def _api_key() -> str:
    value = os.environ.get(ocr.API_KEY_ENV)
    if not value:
        raise RuntimeError(
            f"当前进程没有 {ocr.API_KEY_ENV}；请用隐藏输入在本地终端导出，"
            "不要通过命令行参数或聊天消息传递"
        )
    return value


def _event(kind: str, payload: dict) -> None:
    if kind == "start":
        print(f"OCR 待处理 {payload['todo']} 页，已跳过 {payload['skipped']} 页")
    elif kind == "page_ok":
        print(f"✓ {payload['page']}")
    elif kind == "page_blank":
        print(f"○ {payload['page']}：空白候选，保留原图")
    elif kind == "page_fail":
        print(f"! {payload['page']}：失败，将按状态决定重试或原图兜底", file=sys.stderr)


def _quality_counts(data: dict, selected: set[int] | None = None) -> dict[str, int]:
    counts: dict[str, int] = {}
    for page in data["pages"]:
        if selected is not None and page["source_page"] not in selected:
            continue
        state = page["ocr_state"]
        counts[state] = counts.get(state, 0) + 1
    return counts


def _has_quality_failures(counts: dict[str, int]) -> bool:
    return any(counts.get(state, 0) for state in ("failed", "unsupported", "ambiguous"))


def cmd_init(args: argparse.Namespace) -> int:
    excluded = workflow.parse_page_ranges(args.exclude_pages)
    with workflow.book_lock(args.book_dir):
        data = workflow.initialize_book(
            args.pdf,
            args.book_dir,
            excluded_pages=excluded,
            exclusion_reason=args.exclude_reason,
            dpi=args.dpi,
            ocr_max_dimension=args.ocr_max_dimension,
        )
    print(
        f"已建立页清单：PDF {data['source_pdf']['page_count']} 页，"
        f"纳入 {len(data['pages'])} 页，显式排除 {len(excluded)} 页"
    )
    print(args.book_dir / manifest.MANIFEST_RELATIVE_PATH)
    return 0


def cmd_smoke(args: argparse.Namespace) -> int:
    with workflow.book_lock(args.book_dir):
        data = manifest.load(args.book_dir)
        selected = workflow.representative_pages(data, args.page_count)
        print("代表页：" + ", ".join(str(page) for page in sorted(selected)))
        summary = workflow.run_ocr(
            args.book_dir,
            _api_key(),
            workers=1,
            retries=args.retries,
            max_tokens=args.max_tokens,
            timeout_s=args.timeout,
            selected_pages=selected,
            on_event=_event,
        )
        output = workflow.build_smoke_epub(args.book_dir, selected, title=args.title)
    print(f"代表页测试完成：{output}")
    print(f"OCR 成功 {summary['ok']}，空白 {summary['blank']}，失败 {summary['fail']}")
    print("请人工抽查该 EPUB；确认质量后再运行 `zhpdf2epub run ... --yes`。")
    counts = _quality_counts(manifest.load(args.book_dir), selected)
    return 2 if _has_quality_failures(counts) else 0


def cmd_run(args: argparse.Namespace) -> int:
    if not args.yes:
        raise RuntimeError("完整 OCR 前必须先审阅代表页，并显式传入 --yes")
    with workflow.book_lock(args.book_dir):
        workflow.run_ocr(
            args.book_dir,
            _api_key(),
            workers=args.workers,
            retries=args.retries,
            max_tokens=args.max_tokens,
            timeout_s=args.timeout,
            on_event=_event,
        )
        output, report = workflow.build_book(
            args.book_dir,
            title=args.title,
            author=args.author,
            year=args.year,
            lang=args.lang,
            require_epubcheck=args.require_epubcheck,
            epubcheck_executable=args.epubcheck,
            max_xhtml_bytes=args.max_xhtml_bytes,
        )
    data = manifest.load(args.book_dir, verify_hashes=False)
    representation_counts: dict[str, int] = {}
    for page in data["pages"]:
        representation_counts[page["representation"]] = (
            representation_counts.get(page["representation"], 0) + 1
        )
    print(f"完成：{output}")
    print(
        f"可重排 {representation_counts.get('reflow', 0)} 页；"
        f"混合 {representation_counts.get('hybrid', 0)} 页；"
        f"原图兜底 {representation_counts.get('image', 0)} 页"
    )
    print(f"EPUB SHA-256：{report['epub_sha256']}")
    if not report["epubcheck"]["available"]:
        print("注意：未发现 EPUBCheck；当前只通过内置严格校验，不能称为 EPUBCheck 正式验收。")
    return 2 if _has_quality_failures(_quality_counts(data)) else 0


def cmd_build(args: argparse.Namespace) -> int:
    with workflow.book_lock(args.book_dir):
        output, report = workflow.build_book(
            args.book_dir,
            title=args.title,
            author=args.author,
            year=args.year,
            lang=args.lang,
            require_epubcheck=args.require_epubcheck,
            epubcheck_executable=args.epubcheck,
            max_xhtml_bytes=args.max_xhtml_bytes,
            force_rederive=args.rederive_final,
        )
    data = manifest.load(args.book_dir, verify_hashes=False)
    print(f"重新构建完成：{output}")
    print(f"EPUB SHA-256：{report['epub_sha256']}")
    return 2 if _has_quality_failures(_quality_counts(data)) else 0


def cmd_audit(args: argparse.Namespace) -> int:
    final_dir = args.book_dir / "work/ocr/final"
    texts = {
        int(path.stem.rsplit("_", 1)[1]): path.read_text(encoding="utf-8")
        for path in final_dir.glob("page_*.md")
        if path.is_file()
    }
    report = source_audit.audit(args.book_dir, texts)
    source_audit.write_report(args.book_dir / "work/audit/source-audit.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["exact_duplicate_page_groups"] or report["near_duplicate_text_ranges"] else 0


def cmd_verify(args: argparse.Namespace) -> int:
    epub = args.book_dir / "dist/book.epub"
    data = manifest.load(args.book_dir)
    _, cover_page = workflow.cover_for_book(args.book_dir, data)
    cover_sha256 = next(
        (
            item["archive_sha256"]
            for item in data["pages"]
            if item["source_page"] == cover_page
        ),
        None,
    )
    report = artifact_audit.audit_epub(
        args.book_dir,
        epub,
        max_xhtml_bytes=args.max_xhtml_bytes,
        require_epubcheck=args.require_epubcheck,
        epubcheck_executable=args.epubcheck,
        allowed_cover_sha256=cover_sha256,
    )
    artifact_audit.write_report(args.book_dir / "dist/acceptance.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


def cmd_doctor(_: argparse.Namespace) -> int:
    checks = {
        "python": sys.version.split()[0],
        "pdfinfo": bool(shutil.which("pdfinfo")),
        "pdfimages": bool(shutil.which("pdfimages")),
        "pdftoppm": bool(shutil.which("pdftoppm")),
        "pandoc": bool(shutil.which("pandoc")),
        "epubcheck": bool(shutil.which("epubcheck")),
        "api_key_in_process": bool(os.environ.get(ocr.API_KEY_ENV)),
        "fixed_model": ocr.DEFAULT_MODEL,
    }
    print(json.dumps(checks, ensure_ascii=False, indent=2))
    required = checks["pdfinfo"] and checks["pdfimages"] and checks["pdftoppm"] and checks["pandoc"]
    return 0 if required else 1


def _ocr_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--retries", type=int, default=5, choices=range(0, 6), help="每页最多恢复尝试次数；默认 5，连同首次请求共最多 6 次")
    parser.add_argument("--max-tokens", type=int, default=12_000)
    parser.add_argument("--timeout", type=float, default=300)


def _verify_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-xhtml-bytes", type=int, default=262_144)
    parser.add_argument("--require-epubcheck", action="store_true")
    parser.add_argument("--epubcheck", default=None, help="epubcheck 可执行文件路径；不会自动下载")


def _book_metadata_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--title", required=True)
    parser.add_argument("--author", default=None)
    parser.add_argument("--year", default=None)
    parser.add_argument("--lang", default="zh-CN")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zhpdf2epub", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init", help="导入 PDF 并建立严格页清单")
    init.add_argument("pdf", type=Path)
    init.add_argument("book_dir", type=Path)
    init.add_argument("--dpi", type=int, default=300)
    init.add_argument("--exclude-pages", default=None, help="例如 716 或 1,5-8")
    init.add_argument("--exclude-reason", default=None)
    init.add_argument("--ocr-max-dimension", type=int, default=0, help="0 表示 OCR 使用存档图原字节；大于 0 才生成缩小图")
    init.set_defaults(func=cmd_init)

    smoke = sub.add_parser("smoke", help="OCR 代表页并生成小样 EPUB")
    smoke.add_argument("book_dir", type=Path)
    smoke.add_argument("--title", required=True)
    smoke.add_argument("--page-count", type=int, default=7)
    _ocr_options(smoke)
    smoke.set_defaults(func=cmd_smoke)

    run = sub.add_parser("run", help="完整 OCR、混合构建并严格校验")
    run.add_argument("book_dir", type=Path)
    _book_metadata_options(run)
    run.add_argument("--workers", type=int, default=4)
    run.add_argument("--yes", action="store_true", help="确认已审阅代表页并允许完整 OCR")
    _ocr_options(run)
    _verify_options(run)
    run.set_defaults(func=cmd_run)

    build = sub.add_parser("build", help="不调用 API，基于已冻结 OCR 重新构建 EPUB")
    build.add_argument("book_dir", type=Path)
    _book_metadata_options(build)
    build.add_argument(
        "--rederive-final",
        action="store_true",
        help="显式按当前 corrections.json 重建派生层，并保留旧版本归档",
    )
    _verify_options(build)
    build.set_defaults(func=cmd_build)

    audit = sub.add_parser("audit", help="报告精确/近重复页风险，不自动去重")
    audit.add_argument("book_dir", type=Path)
    audit.set_defaults(func=cmd_audit)

    verify = sub.add_parser("verify", help="重新验证已生成 EPUB")
    verify.add_argument("book_dir", type=Path)
    _verify_options(verify)
    verify.set_defaults(func=cmd_verify)

    doctor = sub.add_parser("doctor", help="只读检查本机依赖和凭据是否存在")
    doctor.set_defaults(func=cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
