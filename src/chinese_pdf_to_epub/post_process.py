"""Post-process stage: merge per-page .md → pandoc-ready book.md.

KHÔNG dùng LLM — pure Python text fix. Trách nhiệm:
1. Merge tất cả page_NNN.md theo thứ tự filename → 1 file book.md
2. Strip ```markdown wrapper nếu model lỡ thêm
3. Renumber footnote cross-page (mỗi page đánh [^1] độc lập → shift theo counter)
4. Detect chapter heading (CHƯƠNG/Chương/PHẦN/Phần/HỒI + số La Mã/Ả Rập/chữ) → h1
5. Inject YAML front matter (title, author, lang) cho pandoc epub metadata

Cross-page hyphen-fix INTENTIONALLY DROPPED.
Lý do: corpus Việt cổ dùng hyphen intentional cho từ ghép ("văn-chương",
"nhân-loại"). Auto-nối khi từ rơi đúng biên page → silent corrupt thành
"vănchương". OCR prompt rule 8 đã handle hyphen trong page.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from .content_policy import requests_page_image, strip_page_image_marker
from .ocr import BLANK_PLACEHOLDER, DEAD_PREFIX, canonical_book_lang, natural_sort_key

# Số viết chữ tiếng Việt cho heading (Hồi/Chương/Phần thứ <chữ>).
# Anchor bằng các từ này để tránh false-positive ("Phần lớn", "Hồi đó").
_VN_ORDINAL = (
    r"(?:nhất|nhị|tam|tứ|ngũ|lục|thất|bát|cửu|thập"
    r"|một|hai|ba|bốn|năm|sáu|bảy|tám|chín|mười"
    r"|mở\s+đầu|kết|cuối|chót)"
)
# Sau từ khoá: hoặc số (La Mã / Ả Rập), hoặc "thứ <chữ>", hoặc trực tiếp <chữ>.
_HEADING_NUM = rf"(?:[\dIVXLCDM]+|thứ\s+{_VN_ORDINAL}|{_VN_ORDINAL})"
_KEYWORDS = r"(?:CHƯƠNG|Chương|PHẦN|Phần|HỒI|Hồi|THIÊN|Thiên|QUYỂN|Quyển)"

# Đuôi hợp lệ SAU keyword+số: hết dòng, HOẶC dấu câu tiêu đề (: . - —) rồi tiêu đề.
# Tiêu đề sau dấu phải không bắt đầu bằng chữ THƯỜNG tiếng Việt (văn xuôi "...của",
# "...với" → loại). Chặn `.*` cũ nuốt cả đoạn văn mở bằng "Phần thứ hai...".
# Lưu ý: nhoa/thường xét THỦ CÔNG (không IGNORECASE) vì IGNORECASE phá phân biệt này.
_LOWER_VN = "a-zàáảãạăằắẳẵặâầấẩẫậeèéẻẽẹêềếểễệiìíỉĩịoòóỏõọôồốổỗộơờớởỡợuùúủũụưừứửữựyỳýỷỹỵđ"
_HEADING_TAIL = rf"(?:\s*$|\s*[:.\-–—]\s*[^{_LOWER_VN}\s].*$)"
# Độ dài tối đa cả dòng heading — heading thật ngắn; đoạn văn dài thì loại.
_HEADING_MAX_LEN = 80

CHAPTER_PATTERNS = [
    re.compile(rf"^\s*({_KEYWORDS}\s+{_HEADING_NUM}\b{_HEADING_TAIL})"),
]


def _is_chapter_heading(line: str) -> bool:
    """True nếu `line` là dòng heading chương thật (không phải văn xuôi mở bằng từ khoá).

    Kết hợp 2 lớp chặn false-positive:
    1. Độ dài: heading thật ngắn (≤ _HEADING_MAX_LEN). Đoạn văn 400 chữ → loại.
    2. Đuôi hợp lệ: sau keyword+số phải hết dòng hoặc dấu câu tiêu đề + tiêu đề
       (không bắt đầu bằng chữ thường tiếng Việt). "Phần thứ hai của..." → loại.
    """
    stripped = line.strip()
    if len(stripped) > _HEADING_MAX_LEN:
        return False
    return any(p.match(stripped) for p in CHAPTER_PATTERNS)

CODE_FENCE_OPEN = re.compile(r"^```(?:markdown|md)?\s*$")
CODE_FENCE_CLOSE = re.compile(r"^```\s*$")

# CommonMark-style fenced code blocks may use any info string, either backticks
# or tildes, and a closing fence at least as long as the opener.  These patterns
# are deliberately separate from CODE_FENCE_OPEN/CLOSE above: those two identify
# only the model's optional outer `````markdown`` wrapper that may be stripped.
_FENCE_CANDIDATE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE_CANDIDATE = re.compile(r"^ {0,3}(`{3,}|~{3,})[ \t]*$")

# Footnote markdown: ref `[^1]` trong body, def `[^1]:` đầu dòng.
_FOOTNOTE_REF = re.compile(r"\[\^(\d+)\]")
_FOOTNOTE_DEF = re.compile(r"^(\s*)\[\^(\d+)\]:\s?(.*)$")


def _fence_opener(line: str) -> tuple[str, int] | None:
    """Return (marker, length) for a valid fenced-code opener, else None."""
    match = _FENCE_CANDIDATE.match(line)
    if match is None:
        return None
    fence, info = match.groups()
    # CommonMark forbids backticks in the info string of a backtick fence.
    if fence[0] == "`" and "`" in info:
        return None
    return fence[0], len(fence)


def _is_fence_closer(line: str, active: tuple[str, int]) -> bool:
    match = _FENCE_CLOSE_CANDIDATE.match(line)
    if match is None:
        return False
    fence = match.group(1)
    return fence[0] == active[0] and len(fence) >= active[1]


def _has_code_indent(line: str) -> bool:
    """Apply CommonMark's four-column tab stops to leading indentation."""
    columns = 0
    for char in line:
        if char == " ":
            columns += 1
        elif char == "\t":
            columns += 4 - (columns % 4)
        else:
            break
        if columns >= 4:
            return True
    return False


def _code_protected_lines(lines: list[str]) -> list[bool]:
    """Mark fenced and four-space/tab-indented code lines as literal content."""
    protected: list[bool] = []
    active: tuple[str, int] | None = None
    for line in lines:
        if active is not None:
            protected.append(True)
            if _is_fence_closer(line, active):
                active = None
            continue
        opener = _fence_opener(line)
        if opener is not None:
            protected.append(True)
            active = opener
            continue
        protected.append(_has_code_indent(line))
    return protected


def renumber_footnotes(text: str, offset: int) -> tuple[str, int]:
    """Cộng `offset` vào mọi số footnote trong page → unique sau khi merge.

    Mỗi page OCR đánh footnote độc lập từ [^1]; merge thẳng sẽ đụng [^1] trùng
    (pandoc chỉ giữ note đầu, "Duplicate note reference"). Shift mỗi page theo
    counter chạy. Returns (text mới, số note distinct trong page) để cộng dồn.

    offset=0 → no-op (giữ nguyên), cho page đầu / page không footnote.
    Bỏ qua [^N] nằm trong fenced code block (``` … ```) — đó là literal code,
    không phải footnote thật.
    """
    seen = set()

    def _shift(m: re.Match) -> str:
        n = int(m.group(1))
        seen.add(n)
        return f"[^{n + offset}]"

    lines = text.splitlines()
    protected = _code_protected_lines(lines)
    out_lines = [
        line if is_protected else _FOOTNOTE_REF.sub(_shift, line)
        for line, is_protected in zip(lines, protected, strict=True)
    ]
    return "\n".join(out_lines), (max(seen) if seen else 0)


def materialize_orphan_footnotes(text: str, lang: str = "vi") -> str:
    """Keep an unreferenced Markdown footnote definition visible in the EPUB.

    Pandoc silently drops ``[^N]: ...`` when no matching ``[^N]`` reference is
    present. Vision OCR may preserve a printed circled marker (for example ``①``)
    in the body while emitting a Markdown definition at the foot of the page.
    Rather than guessing which printed marker should become a link, convert only
    the orphan definition to an ordinary labelled paragraph. Referenced footnotes
    and fenced-code literals remain byte-for-byte unchanged.
    """
    lines = text.splitlines()
    referenced: set[int] = set()
    protected = _code_protected_lines(lines)
    for line, is_protected in zip(lines, protected, strict=True):
        if is_protected or _FOOTNOTE_DEF.match(line):
            continue
        referenced.update(int(match) for match in _FOOTNOTE_REF.findall(line))

    normalized_lang = canonical_book_lang(lang).lower()
    label = "脚注" if normalized_lang.startswith(("zh", "ja")) else "Chú thích"
    out: list[str] = []
    for line, is_protected in zip(lines, protected, strict=True):
        match = None if is_protected else _FOOTNOTE_DEF.match(line)
        if match is None or int(match.group(2)) in referenced:
            out.append(line)
            continue
        number = int(match.group(2))
        body = match.group(3)
        separator = "：" if label == "脚注" else ":"
        out.append(f"{match.group(1)}{label} {number}{separator} {body}".rstrip())
    return "\n".join(out)


def strip_code_fences(text: str) -> str:
    """Bỏ ```markdown wrapper ngoài cùng nếu có."""
    lines = text.splitlines()
    if lines and CODE_FENCE_OPEN.match(lines[0]):
        for i in range(len(lines) - 1, 0, -1):
            if CODE_FENCE_CLOSE.match(lines[i]):
                return "\n".join(lines[1:i])
        return "\n".join(lines[1:])
    return text


# ATX heading thiếu space sau dấu #: `##幽霊の家` (model CJK hay bỏ space ASCII).
# CommonMark BẮT BUỘC space sau # → không có thì pandoc render thành text thường,
# mất heading + mất split point. Chuẩn hoá `#`/`##` (≤6) liền ký tự non-# → chèn space.
_ATX_NO_SPACE = re.compile(r"^(#{1,6})(?=[^#\s])")

# Thematic break kiểu dấu gạch: dòng CHỈ gồm ≥3 dấu `-` (cho phép khoảng trắng xen,
# vd `---`, `----`, `- - -`). OCR bản scan cổ hay sinh dòng gạch ngang phân cách /
# footnote → merged body dính nhiều dòng `---`. pandoc coi `---…---` (sau dòng trống)
# là KHỐI YAML metadata; nếu bên trong có `: ` → rc=64 "mapping values are not allowed"
# (bug thật batch3: 5 cuốn vỡ, vd khai-hung-than-the-va-tac-pham). Đổi sang `* * *`
# (thematic break pandoc KHÔNG nhầm với YAML). Chỉ khớp dạng dấu gạch — `***`/`___`
# vốn đã an toàn nên không đụng.
_DASH_THEMATIC_BREAK = re.compile(r"^\s*-(?:\s*-){2,}\s*$")


def _normalize_thematic_breaks(text: str) -> str:
    """Đổi dòng thematic-break dấu gạch (`---`, `----`, `- - -`) → `* * *`.

    Ngăn pandoc hiểu nhầm `---` giữa body là mở/đóng khối YAML metadata. Bỏ qua dòng
    trong fenced code block (``` … ```) — ở đó `---` là literal, không phải separator.
    """
    lines = text.splitlines()
    protected = _code_protected_lines(lines)
    out_lines = []
    for line, is_protected in zip(lines, protected, strict=True):
        if not is_protected and _DASH_THEMATIC_BREAK.match(line):
            out_lines.append("* * *")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _normalize_atx_heading(stripped: str) -> str:
    """`##幽霊の家` → `## 幽霊の家`. Dòng không phải ATX heading: trả nguyên."""
    return _ATX_NO_SPACE.sub(r"\1 ", stripped)


def upgrade_chapter_headings(text: str) -> str:
    """Detect chapter line, upgrade thành `# Title` (h1, pandoc split point)."""
    lines = text.splitlines()
    protected = _code_protected_lines(lines)
    out_lines = []
    for line, is_protected in zip(lines, protected, strict=True):
        if is_protected:
            out_lines.append(line)
            continue
        stripped = _normalize_atx_heading(line.strip())
        if stripped.startswith(("# ", "## ")):
            if stripped.startswith("## "):
                body = stripped[3:].strip()
                if _is_chapter_heading(body):
                    out_lines.append(f"# {body}")
                    continue
            # giữ heading đã chuẩn-hoá (vd `##幽霊の家`→`## 幽霊の家`), KHÔNG dùng line gốc.
            out_lines.append(stripped)
            continue
        if _is_chapter_heading(stripped):
            out_lines.append(f"# {stripped}")
        else:
            out_lines.append(line)
    return "\n".join(out_lines)


def _yaml_scalar(value: str) -> str:
    """Escape 1 giá trị thành YAML scalar an toàn cho front matter.

    Giá trị nhét thô vỡ pandoc khi chứa ký tự cấu trúc YAML — thực tế: title sách VN
    "Ăn Cơm Mới, Nói Chuyện Cũ: Hậu Giang - Ba Thắc" có dấu `:` → pandoc rc=64
    "mapping values are not allowed". Luôn double-quote + escape `\\` và `"` để phủ mọi
    ký tự đặc biệt (`:`, `#`, `-` đầu dòng, `[`, `{`...) trong 1 cách nhất quán."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


_RESOURCE_LIKE_METADATA = re.compile(r"!\s*\[|\[[^\]\n]*\]\([^\n)]*\)|<[^>\n]+>")


def _metadata_text(value: object, *, field: str, limit: int = 500) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    text = value.strip()
    if not text or len(text) > limit or len(text.splitlines()) != 1:
        raise ValueError(f"{field} must be non-empty single-line text")
    if any(unicodedata.category(char) == "Cc" for char in text):
        raise ValueError(f"{field} must not contain control characters")
    if _RESOURCE_LIKE_METADATA.search(text):
        raise ValueError(f"{field} must be plain text without Markdown/HTML resources")
    if "{" in text or "}" in text:
        raise ValueError(f"{field} must not contain Pandoc attribute syntax")
    return text


def build_front_matter(title: str, author: str | None, lang: str, year: str | None) -> str:
    """Pandoc YAML front matter cho epub metadata."""
    lines = ["---", f"title: {_yaml_scalar(_metadata_text(title, field='title'))}"]
    if author:
        lines.append(f"author: {_yaml_scalar(_metadata_text(author, field='author'))}")
    canonical_lang = _metadata_text(canonical_book_lang(lang), field="lang", limit=35)
    lines.append(f"lang: {_yaml_scalar(canonical_lang)}")
    if year:
        lines.append(f"date: {_yaml_scalar(_metadata_text(str(year), field='year', limit=40))}")
    lines.append("---\n")
    return "\n".join(lines)


def merge_pages(
    *,
    input_dir: Path,
    output_path: Path,
    title: str,
    author: str | None = None,
    lang: str = "vi",
    year: str | None = None,
    pattern: str = "page_*.md",
    scans_dir: Path | None = None,
) -> dict:
    pages = sorted(input_dir.glob(pattern), key=natural_sort_key)
    if not pages:
        raise FileNotFoundError(f"no .md pages found in {input_dir} matching {pattern!r}")

    if scans_dir is None:
        conventional = input_dir.parent.parent / "scans"
        scans_dir = conventional if conventional.is_dir() else None

    chunks = []
    footnote_offset = 0
    preserved_images = 0
    for p in pages:
        raw = p.read_text(encoding="utf-8").strip()
        if not raw:
            continue
        dead_fallback = raw.startswith(DEAD_PREFIX)
        blank_fallback = raw == BLANK_PLACEHOLDER
        preserve_image = requests_page_image(raw) or dead_fallback or blank_fallback
        # A historical dead placeholder is an invisible HTML comment. Preserve
        # the source scan instead so a valid EPUB cannot silently hide the page.
        cleaned = (
            ""
            if dead_fallback or blank_fallback
            else strip_code_fences(strip_page_image_marker(raw)).strip()
        )
        cleaned, page_max = renumber_footnotes(cleaned, footnote_offset)
        cleaned = materialize_orphan_footnotes(cleaned, lang)
        footnote_offset += page_max

        page_chunks: list[str] = []
        if preserve_image:
            if scans_dir is None:
                raise RuntimeError(
                    f"{p.name} requests source-image preservation but scans_dir is unavailable"
                )
            matches = sorted(
                (candidate for candidate in scans_dir.glob(f"{p.stem}.*") if candidate.is_file()),
                key=natural_sort_key,
            )
            if len(matches) != 1:
                raise RuntimeError(
                    f"{p.name} requests source-image preservation but expected exactly one "
                    f"source image, found {len(matches)}"
                )
            rel = Path(os.path.relpath(matches[0], output_path.parent)).as_posix()
            page_chunks.append(
                f"![{p.stem} 原始扫描页，图表或表格视觉信息保留](<{rel}>)"
            )
            preserved_images += 1
        if cleaned:
            page_chunks.append(cleaned)
        if page_chunks:
            chunks.append("\n\n".join(page_chunks))

    merged = "\n\n".join(chunks)
    merged = upgrade_chapter_headings(merged)
    # Đổi thematic-break dấu gạch `---` giữa body → `* * *` (pandoc không nhầm YAML).
    merged = _normalize_thematic_breaks(merged)

    h1_count = sum(1 for line in merged.splitlines() if line.startswith("# "))
    h2_count = sum(1 for line in merged.splitlines() if line.startswith("## "))

    fm = build_front_matter(title, author, lang, year)
    final = fm + "\n" + merged + "\n"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(final, encoding="utf-8")

    return {
        "pages_merged": len(pages),
        "chars": len(final),
        "h1": h1_count,
        "h2": h2_count,
        "footnotes": footnote_offset,
        "preserved_images": preserved_images,
        "output": str(output_path),
    }
