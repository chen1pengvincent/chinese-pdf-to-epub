"""Adversarial tests for bounded and auditable provider requests (no real API)."""

from __future__ import annotations

import base64
import io
import json
import stat
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from email.message import Message
from pathlib import Path
from urllib import error as urlerr

import pytest

from chinese_pdf_to_epub import content_policy, context_prepass, ocr, request_control


def _persistent_reserve(state_path: str, limit: int) -> bool:
    budget = request_control.RequestBudget(
        max_requests=limit,
        max_image_attachments=limit,
        state_path=state_path,
    )
    try:
        budget.reserve(image_attachments=1)
    except request_control.RequestBudgetExceeded:
        return False
    return True


class JsonResponse:
    def __init__(self, body: dict, *, status: int = 200):
        self.raw = json.dumps(body).encode("utf-8")
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = "application/json"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, size: int = -1):
        return self.raw if size < 0 else self.raw[:size]


class SseResponse:
    def __init__(self, lines, *, content_type="text/event-stream", status=200):
        self.lines = iter(lines)
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def readline(self, _size: int = -1):
        return next(self.lines, b"")


def _body(content="OCR", *, finish="stop", reasoning=None, usage=None):
    message = {"content": content}
    if reasoning is not None:
        message["reasoning_content"] = reasoning
    return {
        "id": "resp-safe-1",
        "choices": [{"message": message, "finish_reason": finish}],
        "usage": usage or {"prompt_tokens": 10, "completion_tokens": 2},
    }


def test_zh_prompt_routes_visual_content_to_hybrid_preservation():
    marker = content_policy.PRESERVE_PAGE_IMAGE_MARKER
    assert ocr.ZH_PRESERVE_PAGE_IMAGE_MARKER == marker
    assert f"第一行必须只输出 `{marker}`" in ocr.ZH_PROMPT
    assert "图表、示意图、复杂公式排版" in ocr.ZH_PROMPT
    assert "必须保留行列关系的表格" in ocr.ZH_PROMPT
    unsupported_clause = ocr.ZH_PROMPT.split(
        f"只输出 `{ocr.ZH_UNSUPPORTED_LAYOUT_MARKER}`", 1
    )[0]
    assert "复杂表格" not in unsupported_clause


def test_zh_context_keeps_visual_pages_supported_but_flags_image_preservation():
    prompt = context_prepass.CONTEXT_PROMPT_ZH
    assert "这些内容不得导致 supported_layout=false" in prompt
    assert "layout_notes" in prompt and "ocr_pitfalls" in prompt
    unsupported_bullet = prompt.split("- 不支持：", 1)[1].split("\n", 1)[0]
    assert "复杂表格" not in unsupported_bullet


def test_request_options_are_absent_by_default_and_explicit_when_requested(monkeypatch):
    payloads = []
    timeouts = []

    def fake_open(req, **kwargs):
        payloads.append(json.loads(req.data))
        timeouts.append(kwargs["timeout"])
        return JsonResponse(_body())

    monkeypatch.setattr(ocr, "open_chat_request", fake_open)
    ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100)
    ocr._post_once(
        "k",
        ocr.DEFAULT_MODEL,
        "Yg==",
        "image/png",
        100,
        request_options=request_control.RequestOptions(
            timeout_s=17,
            thinking={"type": "enabled"},
            reasoning_effort="high",
        ),
    )

    assert "stream" not in payloads[0]
    assert "thinking" not in payloads[0]
    assert "reasoning_effort" not in payloads[0]
    assert payloads[1]["thinking"] == {"type": "enabled"}
    assert payloads[1]["reasoning_effort"] == "high"
    assert timeouts == [ocr.REQUEST_TIMEOUT_S, 17]
    assert request_control.RequestOptions().timeout_s == 300


def test_invalid_thinking_combinations_fail_before_network(monkeypatch):
    calls = []
    monkeypatch.setattr(ocr, "open_chat_request", lambda *_a, **_k: calls.append(1))
    with pytest.raises(ValueError):
        request_control.RequestOptions(thinking={"type": "maybe"})
    with pytest.raises(ValueError):
        request_control.RequestOptions(
            thinking={"type": "disabled"}, reasoning_effort="high"
        )
    with pytest.raises(TypeError):
        request_control.RequestOptions(stream="yes")
    assert calls == []


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_request_options_reject_non_finite_numbers(value):
    with pytest.raises(ValueError):
        request_control.RequestOptions(timeout_s=value)
    with pytest.raises(ValueError):
        request_control.RequestOptions(
            timeout_s=10, stream=True, stream_idle_timeout_s=value
        )
    with pytest.raises(ValueError):
        request_control.RequestOptions(timeout_s=10, estimated_cost_usd=value)


def test_safe_ledger_has_required_metadata_but_no_key_content_or_reasoning(
    monkeypatch, tmp_path: Path
):
    secret_key = "ledger-" + "secret-" + "key"
    secret_reasoning = "PRIVATE-" + "REASONING-MUST-NOT-BE-WRITTEN"
    secret_content = "PRIVATE-" + "OCR-CONTENT-MUST-NOT-BE-WRITTEN"
    ledger_path = tmp_path / "request-ledger.jsonl"
    ledger = request_control.RequestLedger(ledger_path)
    budget = request_control.RequestBudget(
        max_requests=1, max_image_attachments=1, max_elapsed_s=60
    )

    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: JsonResponse(
            _body(secret_content, reasoning=secret_reasoning)
        ),
    )
    text, meta = ocr._post_once(
        secret_key,
        ocr.DEFAULT_MODEL,
        "Yg==",
        "image/png",
        100,
        prompt_context="secret context that must only be hashed",
        page="page_001.png",
        page_sha256="a" * 64,
        request_options=request_control.RequestOptions(timeout_s=23),
        budget=budget,
        ledger=ledger,
    )
    assert text == secret_content
    assert meta["reasoning_length"] == len(secret_reasoning)

    raw = ledger_path.read_text(encoding="utf-8")
    assert secret_key not in raw
    assert secret_reasoning not in raw
    assert secret_content not in raw
    assert "secret context" not in raw
    records = [json.loads(line) for line in raw.splitlines()]
    assert [record["event"] for record in records] == ["started", "finished"]
    finished = records[-1]
    assert finished["stage"] == "ocr"
    assert finished["page"] == "page_001.png"
    assert finished["page_sha256"] == "a" * 64
    assert finished["image_count"] == 1
    assert finished["images"][0]["bytes"] == 1
    assert "name" not in finished["images"][0]
    assert finished["model"] == ocr.DEFAULT_MODEL
    assert len(finished["prompt_sha256"]) == 64
    assert len(finished["context_sha256"]) == 64
    assert finished["timeout_s"] == 23
    assert finished["response_id_sha256"] == request_control.sha256_text("resp-safe-1")
    assert finished["thinking_type_requested"] == "omitted"
    assert finished["finish_reason"] == "stop"
    assert finished["usage"]["prompt_tokens"] == 10
    assert finished["reasoning_length"] == len(secret_reasoning)
    assert finished["status"] == "succeeded"
    assert stat.S_IMODE(ledger_path.stat().st_mode) == 0o600


def test_budget_hard_gate_is_thread_safe_and_runs_before_network(monkeypatch):
    budget = request_control.RequestBudget(max_requests=1, max_image_attachments=1)
    calls = {"n": 0}

    def fake_open(*_a, **_k):
        calls["n"] += 1
        return JsonResponse(_body())

    monkeypatch.setattr(ocr, "open_chat_request", fake_open)
    kwargs = {
        "request_options": request_control.RequestOptions(timeout_s=10),
        "budget": budget,
    }
    ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100, **kwargs)
    with pytest.raises(request_control.RequestBudgetExceeded, match="request budget"):
        ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100, **kwargs)
    assert calls["n"] == 1
    assert budget.snapshot()["requests"] == 1
    assert budget.snapshot()["image_attachments"] == 1


def test_budget_concurrent_reservations_never_overshoot():
    budget = request_control.RequestBudget(max_requests=7, max_image_attachments=7)

    def reserve_once(_index):
        try:
            budget.reserve(image_attachments=1)
            return True
        except request_control.RequestBudgetExceeded:
            return False

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve_once, range(100)))
    assert sum(results) == 7
    assert budget.snapshot()["requests"] == 7
    assert budget.snapshot()["image_attachments"] == 7


def test_persistent_budget_survives_restart_and_is_crash_conservative(tmp_path: Path):
    state = tmp_path / "quota.json"
    first = request_control.RequestBudget(
        max_requests=3, max_image_attachments=5, state_path=state
    )
    first.reserve(image_attachments=2)
    # Simulate a process dying after reserve() returned but before any completion
    # record: a fresh object must still see the conservative reservation.
    restarted = request_control.RequestBudget(
        max_requests=3, max_image_attachments=5, state_path=state
    )
    assert restarted.snapshot()["requests"] == 1
    assert restarted.snapshot()["image_attachments"] == 2
    restarted.reserve(image_attachments=3)
    with pytest.raises(request_control.RequestBudgetExceeded, match="attachment"):
        first.reserve(image_attachments=1)
    assert stat.S_IMODE(state.stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(f"{state}.lock").stat().st_mode) == 0o600


def test_persistent_budget_rejects_limit_mismatch_and_corruption(tmp_path: Path):
    state = tmp_path / "quota.json"
    request_control.RequestBudget(max_image_attachments=100, state_path=state)
    with pytest.raises(request_control.RequestBudgetStateError, match="limits differ"):
        request_control.RequestBudget(max_image_attachments=101, state_path=state)
    state.write_text("{broken", encoding="utf-8")
    with pytest.raises(request_control.RequestBudgetStateError, match="malformed"):
        request_control.RequestBudget(max_image_attachments=100, state_path=state)


def test_persistent_budget_is_atomic_across_processes(tmp_path: Path):
    state = tmp_path / "quota.json"
    limit = 7
    request_control.RequestBudget(
        max_requests=limit, max_image_attachments=limit, state_path=state
    )
    with ProcessPoolExecutor(max_workers=8) as pool:
        admitted = list(
            pool.map(
                _persistent_reserve,
                [str(state)] * 30,
                [limit] * 30,
            )
        )
    assert sum(admitted) == limit
    final = request_control.RequestBudget(
        max_requests=limit, max_image_attachments=limit, state_path=state
    )
    assert final.snapshot()["requests"] == limit
    assert final.snapshot()["image_attachments"] == limit


def test_context_multi_image_budget_is_reserved_before_network(monkeypatch):
    budget = request_control.RequestBudget(max_image_attachments=1)
    calls = []
    monkeypatch.setattr(ocr, "open_chat_request", lambda *_a, **_k: calls.append(1))
    with pytest.raises(request_control.RequestBudgetExceeded, match="attachment"):
        context_prepass._post_context_once(
            "k",
            ocr.DEFAULT_MODEL,
            [
                ("YQ==", "image/png", "page_001.png"),
                ("Yg==", "image/png", "page_002.png"),
            ],
            100,
            budget=budget,
        )
    assert calls == []
    assert budget.snapshot()["requests"] == 0


def test_100_image_state_caps_context_retries_concurrency_and_rerun(
    monkeypatch, tmp_path: Path
):
    """No-network integration proof for the qualification-test quota boundary."""
    state = tmp_path / "qualification-100.json"
    sent_attachments: list[int] = []
    sent_lock = threading.Lock()

    def fake_open(req, **_kwargs):
        payload = json.loads(req.data)
        attachment_count = sum(
            1
            for message in payload.get("messages", [])
            for item in message.get("content", [])
            if isinstance(item, dict) and item.get("type") == "image_url"
        )
        with sent_lock:
            call_index = len(sent_attachments)
            sent_attachments.append(attachment_count)
        # Two provider-side 429 attempts model bounded retries.  They are still
        # conservatively charged against the attachment quota.
        if call_index in {1, 2}:
            raise urlerr.HTTPError(
                "https://example.invalid/chat/completions",
                429,
                "rate limited",
                Message(),
                io.BytesIO(b'{"error":"rate limited"}'),
            )
        content = '{"title":"T"}' if attachment_count > 1 else "OCR"
        return JsonResponse(_body(content))

    monkeypatch.setattr(ocr, "open_chat_request", fake_open)
    options = request_control.RequestOptions(timeout_s=10)
    budget = request_control.RequestBudget(
        max_image_attachments=100, state_path=state
    )
    samples = [("Yg==", "image/png", f"page_{i:03d}.png") for i in range(1, 16)]
    context_prepass._post_context_once(
        "k", ocr.DEFAULT_MODEL, samples, 100, request_options=options, budget=budget
    )

    for _ in range(2):
        with pytest.raises(RuntimeError, match="HTTP 429"):
            ocr._post_once(
                "k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100,
                request_options=options, budget=budget,
            )
    for _ in range(7):
        ocr._post_once(
            "k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100,
            request_options=options, budget=budget,
        )

    def full_attempt(_index: int) -> bool:
        try:
            ocr._post_once(
                "k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100,
                request_options=options, budget=budget,
            )
        except request_control.RequestBudgetExceeded:
            return False
        return True

    with ThreadPoolExecutor(max_workers=32) as pool:
        admitted = list(pool.map(full_attempt, range(200)))
    assert sum(admitted) == 76
    assert sum(sent_attachments) == 100
    assert budget.snapshot()["image_attachments"] == 100

    # A fresh runtime with the same explicit state cannot reset the consumed
    # quota, and rejection still happens before fake_open.
    restarted = request_control.RequestBudget(
        max_image_attachments=100, state_path=state
    )
    calls_before = len(sent_attachments)
    with pytest.raises(request_control.RequestBudgetExceeded, match="attachment"):
        ocr._post_once(
            "k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100,
            request_options=options, budget=restarted,
        )
    assert len(sent_attachments) == calls_before


def test_image_dimensions_and_bytes_are_recorded_without_pillow():
    raw = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + (321).to_bytes(4, "big") + (654).to_bytes(4, "big")
    info = request_control.describe_image_b64(
        base64.b64encode(raw).decode("ascii"), "image/png", "page.png"
    )
    assert info["width"] == 321 and info["height"] == 654
    assert info["bytes"] == len(raw)


def test_cost_budget_fails_closed_without_per_request_estimate():
    budget = request_control.RequestBudget(max_estimated_cost_usd=1.0)
    with pytest.raises(request_control.RequestBudgetExceeded, match="estimated_cost_usd"):
        budget.reserve(image_attachments=1)
    assert budget.snapshot()["requests"] == 0


def test_ledger_recursively_drops_text_bearing_nested_fields(tmp_path: Path):
    ledger = request_control.RequestLedger(tmp_path / "ledger.jsonl")
    ledger.record(
        {
            "event": "finished",
            "stage": "ocr",
            "page": "../../SECRET-page.png",
            "images": [
                {
                    "name": "SECRET-name.png",
                    "sha256": "a" * 64,
                    "mime": "image/png",
                    "width": 10,
                    "height": 20,
                    "bytes": 30,
                    "SECRET-child": "must disappear",
                }
            ],
            "usage": {
                "prompt_tokens": 7,
                "SECRET-usage": "must disappear",
                "prompt_tokens_details": {
                    "cached_tokens": 3,
                    "SECRET-detail": "must disappear",
                },
            },
            "response_id": "SECRET-response-id",
            "finish_reason": "SECRET-finish",
            "timestamp": "SECRET-time",
        }
    )
    raw = ledger.path.read_text(encoding="utf-8")
    assert "SECRET" not in raw
    record = json.loads(raw)
    assert record["page"] is None
    assert record["images"] == [
        {
            "sha256": "a" * 64,
            "mime": "image/png",
            "width": 10,
            "height": 20,
            "bytes": 30,
        }
    ]
    assert record["usage"] == {
        "prompt_tokens": 7,
        "prompt_tokens_details": {"cached_tokens": 3},
    }
    assert record["response_id_sha256"] == request_control.sha256_text(
        "SECRET-response-id"
    )
    assert "response_id" not in record and "finish_reason" not in record
    assert request_control.sanitize_usage(
        {"prompt_tokens": 7, "PRIVATE_TEXT_AS_KEY": "secret"}
    ) == {"prompt_tokens": 7}


def test_ledger_hashes_provider_controlled_valid_shape_response_id(tmp_path: Path):
    secret = "resp-" + "SECRET_TEXT_FROM_PROMPT"
    ledger = request_control.RequestLedger(tmp_path / "ledger.jsonl")
    ledger.record(
        {
            "event": "finished",
            "stage": "ocr",
            "response_id": secret,
            "thinking_type_requested": "disabled",
        }
    )
    raw = ledger.path.read_text(encoding="utf-8")
    assert secret not in raw
    record = json.loads(raw)
    assert record["response_id_sha256"] == request_control.sha256_text(secret)
    assert record["thinking_type_requested"] == "disabled"


def test_timeout_ledger_marks_result_and_billing_unknown(monkeypatch, tmp_path: Path):
    ledger_path = tmp_path / "requests.jsonl"
    ledger = request_control.RequestLedger(ledger_path)

    def timeout(*_a, **_k):
        raise TimeoutError("socket timed out")

    monkeypatch.setattr(ocr, "open_chat_request", timeout)
    with pytest.raises(ocr.AmbiguousRequestTimeout):
        ocr._post_once(
            "k",
            ocr.DEFAULT_MODEL,
            "Yg==",
            "image/png",
            100,
            request_options=request_control.RequestOptions(timeout_s=9),
            ledger=ledger,
        )
    finished = json.loads(ledger_path.read_text(encoding="utf-8").splitlines()[-1])
    assert finished["status"] == "timeout_unknown"
    assert finished["result_status"] == "unknown"
    assert finished["billing_status"] == "unknown"
    assert finished["error_type"] == "AmbiguousRequestTimeout"


def test_context_ambiguous_timeout_does_not_enter_retry_loop(monkeypatch):
    calls = {"n": 0}

    def ambiguous(*_args, **_kwargs):
        calls["n"] += 1
        raise ocr.AmbiguousRequestTimeout("result and billing are unknown")

    monkeypatch.setattr(context_prepass, "_post_and_parse_context_once", ambiguous)
    with pytest.raises(ocr.AmbiguousRequestTimeout):
        context_prepass._extract_with_retry(
            "k", ocr.DEFAULT_MODEL, [], 100, retries=4
        )
    assert calls["n"] == 1


def test_ocr_nonempty_finish_length_is_rejected(monkeypatch):
    monkeypatch.setattr(
        ocr, "open_chat_request", lambda *_a, **_k: JsonResponse(_body("partial", finish="length"))
    )
    with pytest.raises(ocr.ResponseTruncatedError, match="finish_reason=length"):
        ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100)


@pytest.mark.parametrize("finish", [None, "content_filter", "tool_calls", "function_call"])
def test_ocr_accepts_only_finish_stop(monkeypatch, finish):
    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: JsonResponse(_body("nonempty", finish=finish)),
    )
    with pytest.raises(ocr.UnexpectedFinishReasonError, match="expected 'stop'"):
        ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100)


@pytest.mark.parametrize("finish", [None, "content_filter", "tool_calls"])
def test_context_accepts_only_finish_stop(monkeypatch, finish):
    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: JsonResponse(_body('{"title":"T"}', finish=finish)),
    )
    with pytest.raises(ocr.UnexpectedFinishReasonError, match="expected 'stop'"):
        context_prepass._post_context_once(
            "k",
            ocr.DEFAULT_MODEL,
            [("Yg==", "image/png", "page_001.png")],
            100,
        )


def test_success_without_usage_is_explicitly_unknown(monkeypatch, tmp_path):
    body = _body("ok")
    body["usage"] = {}
    ledger = request_control.RequestLedger(tmp_path / "requests.jsonl")
    monkeypatch.setattr(
        ocr, "open_chat_request", lambda *_a, **_k: JsonResponse(body)
    )

    _text, meta = ocr._post_once(
        "k",
        ocr.DEFAULT_MODEL,
        "Yg==",
        "image/png",
        100,
        page="page_001.png",
        ledger=ledger,
    )

    assert meta["billing_unknown"] is True
    assert ledger.billing_unknown_count() == 1


def test_context_checks_finish_length_before_empty(monkeypatch):
    monkeypatch.setattr(
        ocr, "open_chat_request", lambda *_a, **_k: JsonResponse(_body("", finish="length"))
    )
    with pytest.raises(ocr.ResponseTruncatedError, match="finish_reason=length"):
        context_prepass._post_context_once(
            "k", ocr.DEFAULT_MODEL, [("Yg==", "image/png", "page_001.png")], 100
        )


def test_context_wraps_urlerror_as_ambiguous_nonretryable_failure(monkeypatch):
    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: (_ for _ in ()).throw(urlerr.URLError("connection reset")),
    )
    with pytest.raises(ocr.AmbiguousRequestTimeout) as exc_info:
        context_prepass._post_context_once(
            "k", ocr.DEFAULT_MODEL, [("Yg==", "image/png", "page_001.png")], 100
        )
    assert not ocr._is_transient(str(exc_info.value))


def test_explicit_sse_stream_combines_chunks_and_requires_done(monkeypatch):
    captured = {}
    lines = [
        b'data: {"id":"s1","choices":[{"delta":{"content":"A","reasoning_content":"xx"},"finish_reason":null}]}\n',
        b"\n",
        b'data: {"id":"s1","choices":[{"delta":{"content":"B"},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}\n',
        b"\n",
        b"data: [DONE]\n",
        b"\n",
    ]

    def fake_open(req, **kwargs):
        captured["payload"] = json.loads(req.data)
        captured["timeout"] = kwargs["timeout"]
        return SseResponse(lines)

    monkeypatch.setattr(ocr, "open_chat_request", fake_open)
    text, meta = ocr._post_once(
        "k",
        ocr.DEFAULT_MODEL,
        "Yg==",
        "image/png",
        100,
        request_options=request_control.RequestOptions(
            timeout_s=20, stream=True, stream_idle_timeout_s=3
        ),
    )
    assert text == "AB"
    assert meta["finish_reason"] == "stop"
    assert meta["reasoning_length"] == 2
    assert captured["payload"]["stream"] is True
    assert captured["payload"]["stream_options"] == {"include_usage": True}
    assert captured["timeout"] == 3


def test_stream_rejects_non_stop_finish_reason():
    lines = [
        b'data: {"id":"s1","choices":[{"delta":{"content":"partial"},"finish_reason":"length"}]}\n',
        b"\n",
        b'data: {"id":"s1","choices":[{"delta":{},"finish_reason":"stop"}]}\n',
        b"\n",
        b"data: [DONE]\n",
        b"\n",
    ]
    with pytest.raises(request_control.StreamProtocolError, match="other than stop"):
        request_control.parse_sse_response(
            SseResponse(lines), http_status=200, total_timeout_s=5
        )


def test_nonstream_response_size_is_bounded_before_json_parse(monkeypatch):
    monkeypatch.setattr(request_control, "MAX_JSON_RESPONSE_BYTES", 32)
    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: JsonResponse(_body(content="甲" * 100)),
    )
    with pytest.raises(request_control.ResponseSizeError, match="16 MiB"):
        ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100)


def test_stream_line_and_total_size_are_bounded(monkeypatch):
    monkeypatch.setattr(request_control, "MAX_SSE_LINE_BYTES", 16)
    with pytest.raises(request_control.ResponseSizeError, match="SSE line"):
        request_control.parse_sse_response(
            SseResponse([b"data: " + b"x" * 32 + b"\n"]),
            http_status=200,
            total_timeout_s=5,
        )


def test_stream_stops_reading_immediately_after_done():
    class KeepAliveAfterDone(SseResponse):
        def __init__(self):
            super().__init__(
                [
                    b'data: {"id":"s1","choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n',
                    b"\n",
                    b"data: [DONE]\n",
                ]
            )
            self.read_count = 0

        def readline(self, size: int = -1):
            self.read_count += 1
            if self.read_count > 3:
                raise AssertionError("parser read after the [DONE] sentinel")
            return super().readline(size)

    response = KeepAliveAfterDone()
    parsed = request_control.parse_sse_response(
        response, http_status=200, total_timeout_s=5
    )
    assert parsed.body["choices"][0]["message"]["content"] == "ok"
    assert response.read_count == 3


def test_stream_json_fallback_is_failed_closed(monkeypatch):
    monkeypatch.setattr(
        ocr,
        "open_chat_request",
        lambda *_a, **_k: SseResponse([], content_type="application/json"),
    )
    with pytest.raises(request_control.StreamProtocolError, match="text/event-stream"):
        ocr._post_once(
            "k",
            ocr.DEFAULT_MODEL,
            "Yg==",
            "image/png",
            100,
            request_options=request_control.RequestOptions(timeout_s=20, stream=True),
        )


def test_stream_read_timeout_is_ambiguous(monkeypatch):
    class TimeoutStream(SseResponse):
        def readline(self, _size: int = -1):
            raise TimeoutError("idle timeout")

    monkeypatch.setattr(ocr, "open_chat_request", lambda *_a, **_k: TimeoutStream([]))
    with pytest.raises(ocr.AmbiguousRequestTimeout):
        ocr._post_once(
            "k",
            ocr.DEFAULT_MODEL,
            "Yg==",
            "image/png",
            100,
            request_options=request_control.RequestOptions(
                timeout_s=20, stream=True, stream_idle_timeout_s=2
            ),
        )


def test_content_part_array_is_supported(monkeypatch):
    body = _body(content=[{"type": "text", "text": "A"}, {"type": "text", "text": "B"}])
    monkeypatch.setattr(ocr, "open_chat_request", lambda *_a, **_k: JsonResponse(body))
    text, _ = ocr._post_once("k", ocr.DEFAULT_MODEL, "Yg==", "image/png", 100)
    assert text == "AB"


def test_run_batch_threads_retry_count_to_each_page(monkeypatch, tmp_path: Path):
    inbox = tmp_path / "in"
    output = tmp_path / "out"
    inbox.mkdir()
    (inbox / "page_001.png").write_bytes(b"\x89PNG\r\n")
    captured = {}

    def fake_ocr_page(*_args, **kwargs):
        captured["retries"] = kwargs["retries"]
        return "ok", {"latency_s": 0.1, "usage": {}}

    monkeypatch.setattr(ocr, "ocr_page", fake_ocr_page)
    summary = ocr.run_batch(
        api_key="k",
        input_dir=inbox,
        output_dir=output,
        model=ocr.DEFAULT_MODEL,
        workers=1,
        pattern="*.png",
        retries=0,
    )
    assert captured["retries"] == 0
    assert summary["ok"] == 1
