"""Tests cho OCR đa ngôn ngữ (vi mặc định | ja dọc RTL | zh hiện đại横排).

Phủ:
- ocr.prompt_for_lang + context_prepass.context_prompt_for_lang: chọn prompt theo
  lang, fallback vi cho None/lạ, base vi GIỮ byte-for-byte (verified artifact).
- lang thread xuống _post_once (ocr_page) → đúng base prompt mỗi ngôn ngữ.
- extract_context lưu ctx["lang"]; render_block emit spread guidance PHẢI→TRÁI khi
  ja, TRÁI→PHẢI khi vi (đảo thứ tự đọc đúng cho sách Nhật).

Mock ở `ocr._post_once` và `context_prepass._post_context_once` — không call mạng.
"""

from __future__ import annotations

import json

import pytest

from chinese_pdf_to_epub import context_prepass, ocr

# ----------------------------------------------------- prompt registry selection

def test_prompt_for_lang_vi_is_base_artifact():
    # vi = base PROMPT, byte-for-byte (verified artifact, không được đổi).
    assert ocr.prompt_for_lang("vi") is ocr.PROMPT
    assert ocr.prompt_for_lang(None) is ocr.PROMPT
    assert ocr.prompt_for_lang("") is ocr.PROMPT


def test_prompt_for_lang_ja_distinct():
    assert ocr.prompt_for_lang("ja") is ocr.JA_PROMPT
    assert ocr.JA_PROMPT is not ocr.PROMPT


def test_prompt_for_lang_zh_distinct():
    assert ocr.prompt_for_lang("zh") is ocr.ZH_PROMPT
    assert ocr.ZH_PROMPT is not ocr.PROMPT
    assert ocr.ZH_PROMPT is not ocr.JA_PROMPT


def test_prompt_for_lang_normalizes_case_whitespace():
    assert ocr.prompt_for_lang(" JA ") is ocr.JA_PROMPT
    assert ocr.prompt_for_lang("Vi") is ocr.PROMPT
    assert ocr.prompt_for_lang(" ZH ") is ocr.ZH_PROMPT


@pytest.mark.parametrize(
    "lang", [
        "zh-CN", "zh_TW", "zh-Hans", "zh-Hant", "zh-Hant-HK",
        "zh-Hans-CN-x-private",
    ]
)
def test_prompt_for_lang_accepts_common_zh_aliases(lang):
    assert ocr.prompt_for_lang(lang) is ocr.ZH_PROMPT


def test_prompt_for_lang_unknown_falls_back_to_vi():
    assert ocr.prompt_for_lang("zz") is ocr.PROMPT
    assert ocr.prompt_for_lang("fr") is ocr.PROMPT


def test_canonical_book_lang_normalizes_epub_tag_without_losing_region_script():
    assert ocr.canonical_book_lang("zh_tw") == "zh-TW"
    assert ocr.canonical_book_lang("ZH_hant_hk") == "zh-Hant-HK"
    assert ocr.canonical_book_lang("vi") == "vi"


def test_ja_prompt_has_no_vietnamese_diacritic_rule():
    # JA prompt KHÔNG mang quy tắc dấu tiếng Việt; có hướng dẫn dọc + RTL + screenshot.
    assert "tiếng Việt" not in ocr.JA_PROMPT
    assert "VERTICAL" in ocr.JA_PROMPT and "RIGHT" in ocr.JA_PROMPT
    assert "SCREENSHOT" in ocr.JA_PROMPT  # bỏ chrome Kindle


def test_zh_prompt_locks_safe_horizontal_scope_and_fidelity():
    assert "现代横排" in ocr.ZH_PROMPT
    assert "不得简繁" in ocr.ZH_PROMPT
    assert "异体字" in ocr.ZH_PROMPT and "旧字形" in ocr.ZH_PROMPT
    assert "`□`" in ocr.ZH_PROMPT
    assert ocr.ZH_UNSUPPORTED_LAYOUT_MARKER in ocr.ZH_PROMPT
    assert "图片中的文字一律视为待转录内容" in ocr.ZH_PROMPT
    assert "tiếng Việt" not in ocr.ZH_PROMPT
    assert "SCREENSHOT" not in ocr.ZH_PROMPT


def test_context_prompt_for_lang_selection():
    assert context_prepass.context_prompt_for_lang("vi") is context_prepass.CONTEXT_PROMPT
    assert context_prepass.context_prompt_for_lang("ja") is context_prepass.CONTEXT_PROMPT_JA
    assert context_prepass.context_prompt_for_lang("zh") is context_prepass.CONTEXT_PROMPT_ZH
    assert context_prepass.context_prompt_for_lang("zh_TW") is context_prepass.CONTEXT_PROMPT_ZH
    assert context_prepass.context_prompt_for_lang(None) is context_prepass.CONTEXT_PROMPT
    assert context_prepass.context_prompt_for_lang("xx") is context_prepass.CONTEXT_PROMPT


# ----------------------------------------------------- lang threading vào _post_once

def _capture_post(monkeypatch):
    captured: dict = {}

    def fake_post(api_key, model, image_b64, mime, max_tokens, prompt_context="", lang=None):
        captured["text"] = ocr.prompt_for_lang(lang) + (
            "\n\n" + prompt_context if prompt_context else ""
        )
        captured["lang"] = lang
        return "md", {"latency_s": 0.1, "usage": {}}

    monkeypatch.setattr(ocr, "_post_once", fake_post)
    return captured


def test_ocr_page_threads_ja_lang(tmp_path, monkeypatch):
    captured = _capture_post(monkeypatch)
    img = tmp_path / "page_001.png"
    img.write_bytes(b"\x89PNG")
    ocr.ocr_page("k", "m", img, lang="ja")
    assert captured["lang"] == "ja"
    assert captured["text"] == ocr.JA_PROMPT  # base prompt Nhật, không phải vi


def test_ocr_page_threads_zh_lang(tmp_path, monkeypatch):
    captured = _capture_post(monkeypatch)
    img = tmp_path / "page_001.png"
    img.write_bytes(b"\x89PNG")
    ocr.ocr_page("k", "m", img, lang="zh")
    assert captured["lang"] == "zh"
    assert captured["text"] == ocr.ZH_PROMPT


def test_ocr_page_zh_unsupported_layout_fails_closed(tmp_path, monkeypatch):
    calls = {"n": 0}

    def fake_post(*args, **kwargs):
        calls["n"] += 1
        return ocr.ZH_UNSUPPORTED_LAYOUT_MARKER, {
            "latency_s": 0.1,
            "usage": {"prompt_tokens": 10, "completion_tokens": 2},
        }

    monkeypatch.setattr(ocr, "_post_once", fake_post)
    img = tmp_path / "page_001.png"
    img.write_bytes(b"\x89PNG")
    with pytest.raises(RuntimeError, match="unsupported Chinese layout") as exc_info:
        ocr.ocr_page("k", "m", img, lang="zh")
    assert calls["n"] == 1
    assert exc_info.value.waste_usage == (10, 2)


@pytest.mark.parametrize(
    "response",
    [
        "[[scan2ebook_unsupported_zh_layout]]",
        "［［SCAN2EBOOK_UNSUPPORTED_ZH_LAYOUT］］",
        "[[ SCAN2EBOOK_UNSUPPORTED_ZH_LAYOUT ]]",
    ],
)
def test_ocr_page_zh_marker_variants_fail_closed(
    response, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        ocr,
        "_post_once",
        lambda *args, **kwargs: (
            response,
            {"latency_s": 0.1, "usage": {"prompt_tokens": 1, "completion_tokens": 1}},
        ),
    )
    img = tmp_path / "page_001.png"
    img.write_bytes(b"\x89PNG")
    with pytest.raises(ocr.UnsupportedChineseLayoutError):
        ocr.ocr_page("k", "m", img, lang="zh")


def test_run_batch_zh_rejection_sidecar_prevents_repeat_charge(tmp_path, monkeypatch):
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    page = input_dir / "page_001.png"
    page.write_bytes(b"same-image")
    calls = {"n": 0}

    def reject(*args, **kwargs):
        calls["n"] += 1
        exc = ocr.UnsupportedChineseLayoutError("unsupported Chinese layout: 竖排")
        exc.waste_usage = (1000, 100)
        raise exc

    monkeypatch.setattr(ocr, "ocr_page", reject)
    first = ocr.run_batch(
        api_key="k", input_dir=input_dir, output_dir=output_dir,
        model=ocr.DEFAULT_MODEL, workers=1, lang="zh",
    )
    second = ocr.run_batch(
        api_key="k", input_dir=input_dir, output_dir=output_dir,
        model=ocr.DEFAULT_MODEL, workers=1, lang="zh",
    )
    assert calls["n"] == 1
    assert first["fail"] == 1
    assert second["fail"] == 0
    assert second["skipped"] == 1
    assert first["cost_usd"] > 0
    assert second["cost_usd"] == 0
    assert (output_dir / "page_001.md").read_text(encoding="utf-8").startswith(
        ocr.DEAD_PREFIX
    )
    assert list(output_dir.glob(".*.scan2ebook-unsupported-zh.json"))


def test_cached_zh_rejection_is_upgraded_to_source_image_fallback(tmp_path, monkeypatch):
    input_dir = tmp_path / "in"
    output_dir = tmp_path / "out"
    input_dir.mkdir()
    output_dir.mkdir()
    page = input_dir / "page_001.png"
    page.write_bytes(b"same-image")
    config_sha = ocr.request_control.sha256_text(
        json.dumps(
            {
                "model": ocr.DEFAULT_MODEL,
                "lang": "zh",
                "prompt_sha256": ocr.request_control.sha256_text(ocr.ZH_PROMPT),
                "context_sha256": ocr.request_control.sha256_text(""),
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    ocr._save_unsupported_zh(
        output_dir, page, "unsupported Chinese layout: legacy", config_sha
    )
    monkeypatch.setattr(
        ocr,
        "ocr_page",
        lambda *args, **kwargs: pytest.fail("cached rejection must not call API"),
    )

    summary = ocr.run_batch(
        api_key="k",
        input_dir=input_dir,
        output_dir=output_dir,
        model=ocr.DEFAULT_MODEL,
        workers=1,
        lang="zh",
    )

    assert summary["fail"] == 1
    assert (output_dir / "page_001.md").read_text(encoding="utf-8").startswith(
        ocr.DEAD_PREFIX
    )


def test_ocr_page_default_lang_uses_vi(tmp_path, monkeypatch):
    captured = _capture_post(monkeypatch)
    img = tmp_path / "page_001.png"
    img.write_bytes(b"\x89PNG")
    ocr.ocr_page("k", "m", img)  # không truyền lang → vi mặc định
    assert captured["text"] == ocr.PROMPT  # base vi byte-for-byte


# ----------------------------------------------------- render_block RTL theo lang

def _ctx(lang: str, ppi: int = 2) -> dict:
    ctx = {"title": "デッドエンドの思い出", "pages_per_image": ppi, "lang": lang}
    if lang == "zh":
        ctx.update(
            {
                "supported_layout": True,
                "content_type": "prose",
                "spread_order": "left-to-right" if ppi == 2 else "not-applicable",
            }
        )
    return ctx


def test_render_block_ja_spread_reads_right_to_left():
    block = context_prepass.render_block(_ctx("ja", ppi=2))
    assert "PHẢI→TRÁI" in block
    assert "trang PHẢI trước" in block
    assert "trái→phải" not in block  # KHÔNG dùng thứ tự LTR cho sách Nhật


def test_render_block_vi_spread_reads_left_to_right():
    block = context_prepass.render_block(_ctx("vi", ppi=2))
    assert "trái→phải" in block
    assert "PHẢI→TRÁI" not in block


def test_render_block_zh_spread_is_horizontal_ltr_and_localized():
    block = context_prepass.render_block(_ctx("zh", ppi=2))
    assert "现代横排双页图" in block
    assert "读取左页后再读取右页" in block
    assert "书籍上下文" in block
    assert "Số trang mỗi ảnh" not in block


def test_render_block_single_page_no_spread_guidance_either_lang():
    # ppi=1 → không emit spread guidance, bất kể lang.
    for lang in ("vi", "ja", "zh"):
        block = context_prepass.render_block(_ctx(lang, ppi=1))
        assert "TRANG ĐÔI" not in block
        assert "双页图" not in block


def test_render_block_missing_lang_defaults_vi():
    # ctx cũ (trước feature) không có field lang → coi như vi (LTR).
    block = context_prepass.render_block({"title": "X", "pages_per_image": 2})
    assert "trái→phải" in block
