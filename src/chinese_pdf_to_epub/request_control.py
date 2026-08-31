"""Bounded, auditable controls for OpenAI-compatible image requests.

This module intentionally knows nothing about credentials or response text.  It
stores only request/response metadata that is safe to persist and provides a
thread-safe hard gate before network I/O, with optional file-backed coordination
across processes and reruns.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import struct
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:  # POSIX/macOS
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised on Windows hosts
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - exercised on POSIX hosts
    _msvcrt = None


class RequestBudgetExceeded(RuntimeError):
    """A configured request budget would be exceeded before network I/O."""


class RequestBudgetStateError(ValueError):
    """A persistent budget state is corrupt or belongs to different limits."""


class StreamProtocolError(RuntimeError):
    """A stream request did not return a complete, valid SSE response."""


class ResponseSizeError(RuntimeError):
    """A provider response exceeded the local memory-safety limit."""


MAX_JSON_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_SSE_LINE_BYTES = 4 * 1024 * 1024
MAX_SSE_TOTAL_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class RequestOptions:
    """Explicit transport/provider options for one request.

    ``thinking`` and ``reasoning_effort`` are omitted from the payload unless the
    caller explicitly supplies them.  This preserves the provider's current
    default contract and does not assume that optional parameters are forwarded.

    ``timeout_s`` is a urllib socket-I/O timeout.  For streams it is also checked
    as a best-effort deadline after response headers, between SSE reads; it is not
    a strict end-to-end wall-clock deadline because a blocking ``readline`` can
    only be interrupted by the socket timeout.
    """

    timeout_s: float = 300.0
    stream: bool = False
    stream_idle_timeout_s: float | None = None
    thinking: dict[str, str] | None = None
    reasoning_effort: str | None = None
    estimated_cost_usd: float | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.stream, bool):
            raise TypeError("stream must be a boolean")
        if (
            isinstance(self.timeout_s, bool)
            or not isinstance(self.timeout_s, (int, float))
            or not math.isfinite(self.timeout_s)
            or self.timeout_s <= 0
        ):
            raise ValueError("timeout_s must be a finite number > 0")
        if self.stream_idle_timeout_s is not None and (
            isinstance(self.stream_idle_timeout_s, bool)
            or not isinstance(self.stream_idle_timeout_s, (int, float))
            or not math.isfinite(self.stream_idle_timeout_s)
            or self.stream_idle_timeout_s <= 0
        ):
            raise ValueError("stream_idle_timeout_s must be a finite number > 0")
        if not self.stream and self.stream_idle_timeout_s is not None:
            raise ValueError("stream_idle_timeout_s requires stream=True")
        if self.thinking is not None:
            if not isinstance(self.thinking, dict) or set(self.thinking) != {"type"}:
                raise ValueError("thinking must be exactly {'type': 'enabled'|'disabled'}")
            if self.thinking["type"] not in {"enabled", "disabled"}:
                raise ValueError("thinking.type must be 'enabled' or 'disabled'")
        if self.reasoning_effort not in {None, "low", "high", "max"}:
            raise ValueError("reasoning_effort must be one of: low, high, max")
        if (
            self.thinking is not None
            and self.thinking.get("type") == "disabled"
            and self.reasoning_effort is not None
        ):
            raise ValueError("reasoning_effort cannot be set when thinking is disabled")
        if self.estimated_cost_usd is not None and (
            isinstance(self.estimated_cost_usd, bool)
            or not isinstance(self.estimated_cost_usd, (int, float))
            or not math.isfinite(self.estimated_cost_usd)
            or self.estimated_cost_usd < 0
        ):
            raise ValueError("estimated_cost_usd must be a finite number >= 0")

    @property
    def socket_timeout_s(self) -> float:
        """Socket-I/O timeout passed to urllib; streams use the stricter idle value."""
        if self.stream and self.stream_idle_timeout_s is not None:
            return min(self.timeout_s, self.stream_idle_timeout_s)
        return self.timeout_s


def _write_all(fd: int, value: bytes) -> None:
    pending = memoryview(value)
    while pending:
        written = os.write(fd, pending)
        if written <= 0:
            raise OSError("file write made no progress")
        pending = pending[written:]


def _open_nofollow(path: Path, flags: int, mode: int = 0o600) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), mode)


def _restrict_fd(fd: int) -> None:
    try:
        os.fchmod(fd, 0o600)
    except (AttributeError, OSError):  # Windows ACLs are not represented as POSIX mode
        pass


@contextmanager
def _exclusive_file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = _open_nofollow(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        _restrict_fd(fd)
        if _fcntl is not None:
            _fcntl.flock(fd, _fcntl.LOCK_EX)
        elif _msvcrt is not None:  # pragma: no cover - Windows-only branch
            if os.fstat(fd).st_size == 0:
                os.write(fd, b"\0")
                os.fsync(fd)
            os.lseek(fd, 0, os.SEEK_SET)
            _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - unsupported interpreter platform
            raise RequestBudgetStateError(
                "no cross-process file-lock primitive is available"
            )
        yield
    finally:
        try:
            if _fcntl is not None:
                _fcntl.flock(fd, _fcntl.LOCK_UN)
            elif _msvcrt is not None:  # pragma: no cover - Windows-only branch
                os.lseek(fd, 0, os.SEEK_SET)
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd = _open_nofollow(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        _restrict_fd(fd)
        _write_all(fd, encoded)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    else:
        os.close(fd)
    try:
        os.replace(tmp, path)
        os.chmod(path, 0o600, follow_symlinks=False)
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        tmp.unlink(missing_ok=True)


class RequestBudget:
    """Thread-safe hard gate, optionally durable across processes and reruns.

    Reservations are never released and a persistent reservation is atomically
    fsynced before network I/O.  A crash therefore conservatively consumes the
    request/image/cost quota rather than risking an unrecorded duplicate call.
    A stable sidecar lock serializes all readers/writers while the state itself is
    replaced atomically.  Existing state with different limits fails closed.
    """

    _STATE_SCHEMA = 1

    def __init__(
        self,
        *,
        max_requests: int | None = None,
        max_image_attachments: int | None = None,
        max_elapsed_s: float | None = None,
        max_estimated_cost_usd: float | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        for name, value in {
            "max_requests": max_requests,
            "max_image_attachments": max_image_attachments,
        }.items():
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer")
        for name, value in {
            "max_elapsed_s": max_elapsed_s,
            "max_estimated_cost_usd": max_estimated_cost_usd,
        }.items():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
            ):
                raise ValueError(f"{name} must be a finite number >= 0")
        self.max_requests = max_requests
        self.max_image_attachments = max_image_attachments
        self.max_elapsed_s = max_elapsed_s
        self.max_estimated_cost_usd = max_estimated_cost_usd
        self.state_path = Path(state_path).expanduser() if state_path is not None else None
        self._state_lock_path = (
            Path(f"{self.state_path}.lock") if self.state_path is not None else None
        )
        self._limits = {
            "max_requests": max_requests,
            "max_image_attachments": max_image_attachments,
            "max_elapsed_s": max_elapsed_s,
            "max_estimated_cost_usd": max_estimated_cost_usd,
        }
        self._started = time.monotonic()
        self._requests = 0
        self._images = 0
        self._estimated_cost = 0.0
        self._lock = threading.Lock()
        if self.state_path is not None:
            self._initialize_state()

    def _initial_state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "schema_version": self._STATE_SCHEMA,
            "limits": self._limits,
            "started_at_epoch_s": now,
            "counters": {
                "requests": 0,
                "image_attachments": 0,
                "estimated_cost_usd": 0.0,
            },
            "updated_at": _utc_now(),
        }

    def _read_state_locked(self) -> dict[str, Any]:
        assert self.state_path is not None
        if self.state_path.is_symlink():
            raise RequestBudgetStateError("request budget state must not be a symlink")
        try:
            fd = _open_nofollow(self.state_path, os.O_RDONLY)
        except FileNotFoundError as exc:
            raise RequestBudgetStateError("request budget state disappeared") from exc
        try:
            size = os.fstat(fd).st_size
            if size <= 0 or size > 64 * 1024:
                raise RequestBudgetStateError("invalid request budget state size")
            chunks: list[bytes] = []
            remaining = size
            while remaining:
                chunk = os.read(fd, min(remaining, 8192))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        finally:
            os.close(fd)
        try:
            state = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RequestBudgetStateError("malformed request budget state") from exc
        self._validate_state(state)
        return state

    def _validate_state(self, state: Any) -> None:
        if not isinstance(state, dict) or state.get("schema_version") != self._STATE_SCHEMA:
            raise RequestBudgetStateError("unsupported request budget state schema")
        if set(state) != {
            "schema_version",
            "limits",
            "started_at_epoch_s",
            "counters",
            "updated_at",
        }:
            raise RequestBudgetStateError("invalid request budget state fields")
        if state.get("limits") != self._limits:
            raise RequestBudgetStateError(
                "request budget state limits differ from the current configuration"
            )
        counters = state.get("counters")
        if not isinstance(counters, dict) or set(counters) != {
            "requests",
            "image_attachments",
            "estimated_cost_usd",
        }:
            raise RequestBudgetStateError("invalid request budget state counters")
        for key in ("requests", "image_attachments"):
            value = counters.get(key)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RequestBudgetStateError(f"invalid request budget counter: {key}")
        cost = counters.get("estimated_cost_usd")
        if (
            isinstance(cost, bool)
            or not isinstance(cost, (int, float))
            or not math.isfinite(cost)
            or cost < 0
        ):
            raise RequestBudgetStateError("invalid estimated cost counter")
        started = state.get("started_at_epoch_s")
        if (
            isinstance(started, bool)
            or not isinstance(started, (int, float))
            or not math.isfinite(started)
            or started <= 0
        ):
            raise RequestBudgetStateError("invalid request budget start time")
        updated = state.get("updated_at")
        if not isinstance(updated, str) or len(updated) > 40 or "T" not in updated:
            raise RequestBudgetStateError("invalid request budget update time")

    def _sync_local(self, counters: dict[str, Any]) -> None:
        self._requests = counters["requests"]
        self._images = counters["image_attachments"]
        self._estimated_cost = float(counters["estimated_cost_usd"])

    def _initialize_state(self) -> None:
        assert self.state_path is not None and self._state_lock_path is not None
        with self._lock, _exclusive_file_lock(self._state_lock_path):
            if self.state_path.exists() or self.state_path.is_symlink():
                state = self._read_state_locked()
            else:
                state = self._initial_state()
                _atomic_write_json(self.state_path, state)
            self._sync_local(state["counters"])

    def _elapsed_from_state(self, state: dict[str, Any]) -> float:
        wall_elapsed = time.time() - float(state["started_at_epoch_s"])
        if wall_elapsed < -1.0:
            raise RequestBudgetStateError(
                "system clock precedes request budget state start time"
            )
        return max(0.0, wall_elapsed, time.monotonic() - self._started)

    def _reserve_values(
        self,
        *,
        counters: dict[str, Any],
        elapsed: float,
        image_attachments: int,
        estimated_cost_usd: float | None,
    ) -> None:
        if self.max_elapsed_s is not None and elapsed >= self.max_elapsed_s:
            raise RequestBudgetExceeded(
                f"request budget elapsed limit reached ({self.max_elapsed_s}s)"
            )
        if self.max_requests is not None and counters["requests"] + 1 > self.max_requests:
            raise RequestBudgetExceeded(
                f"request budget exhausted ({self.max_requests} requests)"
            )
        if (
            self.max_image_attachments is not None
            and counters["image_attachments"] + image_attachments
            > self.max_image_attachments
        ):
            raise RequestBudgetExceeded(
                "image attachment budget exhausted "
                f"({self.max_image_attachments} attachments)"
            )
        if self.max_estimated_cost_usd is not None:
            if estimated_cost_usd is None:
                raise RequestBudgetExceeded(
                    "estimated_cost_usd is required when a cost budget is configured"
                )
            if (
                counters["estimated_cost_usd"] + estimated_cost_usd
                > self.max_estimated_cost_usd + 1e-12
            ):
                raise RequestBudgetExceeded(
                    "estimated cost budget exhausted "
                    f"(${self.max_estimated_cost_usd:.6f})"
                )
        counters["requests"] += 1
        counters["image_attachments"] += image_attachments
        counters["estimated_cost_usd"] += estimated_cost_usd or 0.0

    def reserve(
        self, *, image_attachments: int, estimated_cost_usd: float | None = None
    ) -> dict[str, float | int]:
        if (
            isinstance(image_attachments, bool)
            or not isinstance(image_attachments, int)
            or image_attachments < 0
        ):
            raise ValueError("image_attachments must be a non-negative integer")
        if estimated_cost_usd is not None and (
            isinstance(estimated_cost_usd, bool)
            or not isinstance(estimated_cost_usd, (int, float))
            or not math.isfinite(estimated_cost_usd)
            or estimated_cost_usd < 0
        ):
            raise ValueError("estimated_cost_usd must be a finite number >= 0")
        with self._lock:
            if self.state_path is None:
                elapsed = time.monotonic() - self._started
                counters = {
                    "requests": self._requests,
                    "image_attachments": self._images,
                    "estimated_cost_usd": self._estimated_cost,
                }
                self._reserve_values(
                    counters=counters,
                    elapsed=elapsed,
                    image_attachments=image_attachments,
                    estimated_cost_usd=estimated_cost_usd,
                )
                self._sync_local(counters)
                return self._snapshot_unlocked(elapsed)

            assert self._state_lock_path is not None
            with _exclusive_file_lock(self._state_lock_path):
                state = self._read_state_locked()
                elapsed = self._elapsed_from_state(state)
                counters = dict(state["counters"])
                self._reserve_values(
                    counters=counters,
                    elapsed=elapsed,
                    image_attachments=image_attachments,
                    estimated_cost_usd=estimated_cost_usd,
                )
                state["counters"] = counters
                state["updated_at"] = _utc_now()
                # Durable before returning to RequestAttempt, which sends only after
                # this reservation succeeds.
                _atomic_write_json(self.state_path, state)
                self._sync_local(counters)
                return self._snapshot_unlocked(elapsed)

    def _snapshot_unlocked(self, elapsed: float | None = None) -> dict[str, float | int]:
        return {
            "requests": self._requests,
            "image_attachments": self._images,
            "elapsed_s": round(
                time.monotonic() - self._started if elapsed is None else elapsed, 6
            ),
            "estimated_cost_usd": round(self._estimated_cost, 8),
        }

    def snapshot(self) -> dict[str, float | int]:
        with self._lock:
            if self.state_path is None:
                return self._snapshot_unlocked()
            assert self._state_lock_path is not None
            with _exclusive_file_lock(self._state_lock_path):
                state = self._read_state_locked()
                self._sync_local(state["counters"])
                return self._snapshot_unlocked(self._elapsed_from_state(state))


_PAGE_RE = re.compile(r"^page_\d+\.(?:png|jpe?g|webp|heic|heif)$", re.IGNORECASE)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_LEDGER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")
_ERROR_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,99}$")
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"}
_FINISH_REASONS = {"stop", "length", "content_filter", "tool_calls", "function_call"}
_EVENTS = {"started", "finished"}
_STAGES = {"ocr", "context", "cover"}
_STATUSES = {"started", "succeeded", "failed", "timeout_unknown"}
_RESULT_STATUSES = {"unknown", "known", "failed"}
_BILLING_STATUSES = {"unknown", "known"}
_THINKING_TYPES = {"enabled", "disabled", "omitted"}
_USAGE_KEYS = {
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "input_tokens",
    "output_tokens",
}
_USAGE_DETAIL_KEYS = {
    "cached_tokens",
    "audio_tokens",
    "reasoning_tokens",
    "accepted_prediction_tokens",
    "rejected_prediction_tokens",
    "text_tokens",
    "image_tokens",
}
_USAGE_DETAIL_CONTAINERS = {
    "prompt_tokens_details",
    "completion_tokens_details",
    "input_tokens_details",
    "output_tokens_details",
}


def _safe_number(value: Any, *, minimum: float = 0) -> int | float | None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < minimum
    ):
        return None
    return value


def sanitize_usage(usage: Any) -> dict[str, Any]:
    """Allow only documented token counters and fixed numeric detail keys."""
    if not isinstance(usage, dict):
        return {}
    clean: dict[str, Any] = {}
    for key in _USAGE_KEYS:
        value = _safe_number(usage.get(key))
        if value is not None:
            clean[key] = value
    for container in _USAGE_DETAIL_CONTAINERS:
        source = usage.get(container)
        if not isinstance(source, dict):
            continue
        details: dict[str, int | float] = {}
        for key in _USAGE_DETAIL_KEYS:
            value = _safe_number(source.get(key))
            if value is not None:
                details[key] = value
        if details:
            clean[container] = details
    return clean


def _sanitize_images(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in value[:1000]:
        if not isinstance(item, dict):
            continue
        sha = item.get("sha256")
        mime = item.get("mime")
        byte_count = _safe_number(item.get("bytes"))
        if not isinstance(sha, str) or not _HASH_RE.fullmatch(sha.lower()):
            continue
        if mime not in _IMAGE_MIMES or not isinstance(byte_count, int):
            continue
        image: dict[str, Any] = {
            "sha256": sha.lower(),
            "mime": mime,
            "bytes": byte_count,
        }
        for dimension in ("width", "height"):
            raw = item.get(dimension)
            if raw is None:
                image[dimension] = None
            else:
                number = _safe_number(raw, minimum=1)
                image[dimension] = number if isinstance(number, int) else None
        clean.append(image)
    return clean


def _sanitize_budget_snapshot(value: Any) -> dict[str, int | float] | None:
    if not isinstance(value, dict):
        return None
    clean: dict[str, int | float] = {}
    for key in ("requests", "image_attachments"):
        number = _safe_number(value.get(key))
        if isinstance(number, int):
            clean[key] = number
    for key in ("elapsed_s", "estimated_cost_usd"):
        number = _safe_number(value.get(key))
        if number is not None:
            clean[key] = number
    return clean or None


def _sanitize_ledger_record(fields: Any) -> dict[str, Any]:
    source = fields if isinstance(fields, dict) else {}
    record: dict[str, Any] = {
        "schema_version": 1,
        "timestamp": _utc_now(),
    }

    def enum_field(key: str, allowed: set[str]) -> None:
        value = source.get(key)
        if isinstance(value, str) and value in allowed:
            record[key] = value

    enum_field("event", _EVENTS)
    enum_field("stage", _STAGES)
    enum_field("status", _STATUSES)
    enum_field("result_status", _RESULT_STATUSES)
    enum_field("billing_status", _BILLING_STATUSES)
    enum_field("finish_reason", _FINISH_REASONS)
    enum_field("thinking_type_requested", _THINKING_TYPES)

    ledger_id = source.get("ledger_id")
    if isinstance(ledger_id, str) and _LEDGER_ID_RE.fullmatch(ledger_id):
        record["ledger_id"] = ledger_id
    page = source.get("page")
    if isinstance(page, str) and _PAGE_RE.fullmatch(page):
        record["page"] = page
    else:
        record["page"] = None
    for key in ("page_sha256", "prompt_sha256", "context_sha256"):
        value = source.get(key)
        if isinstance(value, str) and _HASH_RE.fullmatch(value.lower()):
            record[key] = value.lower()
        elif key == "context_sha256":
            record[key] = None
    images = _sanitize_images(source.get("images"))
    if "images" in source:
        record["images"] = images
        record["image_count"] = len(images)
    model = source.get("model")
    if isinstance(model, str) and _MODEL_RE.fullmatch(model):
        record["model"] = model
    response_id = source.get("response_id")
    # Provider-controlled IDs are not safe to persist verbatim: even a value with
    # a conventional ``resp-...`` shape can embed prompt text or a credential.
    # A hash preserves correlation without making the ledger a text exfiltration
    # channel.
    if isinstance(response_id, str) and response_id:
        record["response_id_sha256"] = sha256_text(response_id)
    error_type = source.get("error_type")
    if isinstance(error_type, str) and _ERROR_TYPE_RE.fullmatch(error_type):
        record["error_type"] = error_type
    effort = source.get("reasoning_effort_requested")
    if effort in {None, "low", "high", "max"}:
        record["reasoning_effort_requested"] = effort
    if isinstance(source.get("stream"), bool):
        record["stream"] = source["stream"]
    for key in ("max_tokens", "reasoning_length"):
        value = _safe_number(source.get(key))
        if isinstance(value, int):
            record[key] = value
    for key in (
        "timeout_s",
        "stream_idle_timeout_s",
        "estimated_cost_usd",
        "elapsed_s",
    ):
        value = source.get(key)
        if value is None and key == "stream_idle_timeout_s":
            record[key] = None
            continue
        number = _safe_number(value)
        if number is not None:
            record[key] = number
    http_status = source.get("http_status")
    if isinstance(http_status, int) and not isinstance(http_status, bool) and 100 <= http_status <= 599:
        record["http_status"] = http_status
    usage = sanitize_usage(source.get("usage"))
    if usage or "usage" in source:
        record["usage"] = usage
    budget_snapshot = _sanitize_budget_snapshot(source.get("budget_snapshot"))
    if budget_snapshot is not None:
        record["budget_snapshot"] = budget_snapshot
    return record


class RequestLedger:
    """Cross-process append-only JSONL ledger with recursive safe-field filtering."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()
        self._lock_path = Path(f"{self.path}.lock")
        self._lock = threading.Lock()

    def record(self, fields: dict[str, Any]) -> None:
        record = _sanitize_ledger_record(fields)
        encoded = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode(
            "utf-8"
        )
        with self._lock, _exclusive_file_lock(self._lock_path):
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # O_APPEND plus the process lock prevents interleaved lines. fsync makes
            # the "started" record durable before the network call begins.
            fd = _open_nofollow(
                self.path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600
            )
            try:
                _restrict_fd(fd)
                _write_all(fd, encoded)
                os.fsync(fd)
            finally:
                os.close(fd)

    def billing_unknown_count(self) -> int:
        """Count completed attempts whose provider billing is still unknown."""
        if not self.path.exists():
            return 0
        count = 0
        with self._lock, _exclusive_file_lock(self._lock_path):
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except OSError:
                return 0
        for line in lines:
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                # Ledger corruption must not create a false zero.
                return max(1, count)
            if (
                isinstance(record, dict)
                and record.get("event") == "finished"
                and record.get("billing_status") == "unknown"
            ):
                count += 1
        return count


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _image_dimensions(raw: bytes, mime: str) -> tuple[int | None, int | None]:
    """Read dimensions for PNG/JPEG/WebP without adding an image dependency."""
    if mime == "image/png" and len(raw) >= 24 and raw.startswith(b"\x89PNG\r\n\x1a\n"):
        return struct.unpack(">II", raw[16:24])
    if mime == "image/jpeg" and raw.startswith(b"\xff\xd8"):
        pos = 2
        while pos + 9 <= len(raw):
            if raw[pos] != 0xFF:
                pos += 1
                continue
            marker = raw[pos + 1]
            pos += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if pos + 2 > len(raw):
                break
            size = int.from_bytes(raw[pos : pos + 2], "big")
            if size < 2 or pos + size > len(raw):
                break
            if marker in {
                0xC0,
                0xC1,
                0xC2,
                0xC3,
                0xC5,
                0xC6,
                0xC7,
                0xC9,
                0xCA,
                0xCB,
                0xCD,
                0xCE,
                0xCF,
            } and size >= 7:
                height = int.from_bytes(raw[pos + 3 : pos + 5], "big")
                width = int.from_bytes(raw[pos + 5 : pos + 7], "big")
                return width, height
            pos += size
    if mime == "image/webp" and len(raw) >= 30 and raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        kind = raw[12:16]
        if kind == b"VP8X":
            width = 1 + int.from_bytes(raw[24:27], "little")
            height = 1 + int.from_bytes(raw[27:30], "little")
            return width, height
    return None, None


def describe_image_b64(encoded: str, mime: str, name: str | None = None) -> dict[str, Any]:
    # ``name`` remains in the call signature for compatibility but is deliberately
    # not returned: filenames are not needed for billing/audit and may contain text.
    del name
    raw = base64.b64decode(encoded, validate=True)
    width, height = _image_dimensions(raw, mime)
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "mime": mime,
        "width": width,
        "height": height,
        "bytes": len(raw),
    }
def apply_request_options(payload: dict[str, Any], options: RequestOptions) -> dict[str, Any]:
    """Return a shallow payload copy containing only explicitly requested options."""
    result = dict(payload)
    if options.stream:
        result["stream"] = True
        result["stream_options"] = {"include_usage": True}
    if options.thinking is not None:
        result["thinking"] = dict(options.thinking)
    if options.reasoning_effort is not None:
        result["reasoning_effort"] = options.reasoning_effort
    return result


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if not isinstance(value, list):
        return ""
    chunks: list[str] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            chunks.append(text)
    return "".join(chunks)


def _reasoning_length(message: Any) -> int:
    if not isinstance(message, dict):
        return 0

    def text_length(value: Any) -> int:
        if isinstance(value, str):
            return len(value)
        if isinstance(value, list):
            return sum(text_length(item) for item in value)
        if isinstance(value, dict):
            return sum(text_length(item) for item in value.values())
        return 0

    total = 0
    for key in ("reasoning_content", "reasoning"):
        total += text_length(message.get(key))
    return total


@dataclass(frozen=True)
class ChatResponse:
    body: dict[str, Any]
    http_status: int | None
    reasoning_length: int


def parse_json_response(raw: bytes, *, http_status: int | None) -> ChatResponse:
    if len(raw) > MAX_JSON_RESPONSE_BYTES:
        raise ResponseSizeError("chat JSON response exceeded the 16 MiB limit")
    body = json.loads(raw.decode("utf-8"))
    if not isinstance(body, dict):
        raise TypeError("chat response must be a JSON object")
    choices = body.get("choices")
    reasoning_length = 0
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reasoning_length = _reasoning_length(choices[0].get("message"))
    return ChatResponse(body, http_status, reasoning_length)


def _content_type(response: Any) -> str:
    headers = getattr(response, "headers", None)
    if headers is None:
        return ""
    try:
        return str(headers.get_content_type()).lower()
    except (AttributeError, TypeError, ValueError):
        value = headers.get("Content-Type", "") if hasattr(headers, "get") else ""
        return str(value).split(";", 1)[0].strip().lower()


def parse_sse_response(
    response: Any, *, http_status: int | None, total_timeout_s: float
) -> ChatResponse:
    """Parse OpenAI-compatible SSE, failing closed on truncation or non-stop.

    ``total_timeout_s`` is a best-effort post-header deadline checked between
    reads.  The socket timeout configured by the caller is what bounds a single
    blocking ``readline``.
    """
    if _content_type(response) != "text/event-stream":
        raise StreamProtocolError("stream request did not return text/event-stream")
    started = time.monotonic()
    data_lines: list[str] = []
    content: list[str] = []
    reasoning_length = 0
    response_id = None
    response_model = None
    finish_reason = None
    usage: dict[str, Any] = {}
    saw_event = False
    saw_done = False
    total_bytes = 0

    def process_event() -> None:
        nonlocal reasoning_length, response_id, response_model, finish_reason, usage
        nonlocal saw_event, saw_done
        if not data_lines:
            return
        data = "\n".join(data_lines).strip()
        data_lines.clear()
        if data == "[DONE]":
            if finish_reason != "stop":
                raise StreamProtocolError(
                    "stream completed without the only accepted finish_reason=stop"
                )
            saw_done = True
            return
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as exc:
            raise StreamProtocolError("stream contained malformed JSON data") from exc
        if not isinstance(chunk, dict):
            raise StreamProtocolError("stream JSON event must be an object")
        saw_event = True
        response_id = response_id or chunk.get("id")
        response_model = response_model or chunk.get("model")
        if isinstance(chunk.get("usage"), dict):
            usage = chunk["usage"]
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                text = _content_text(delta.get("content"))
                if text:
                    content.append(text)
                reasoning_length += _reasoning_length(delta)
            choice_finish = choice.get("finish_reason")
            if choice_finish is not None:
                if choice_finish != "stop":
                    raise StreamProtocolError(
                        "stream contained a finish_reason other than stop"
                    )
                finish_reason = "stop"

    while True:
        if time.monotonic() - started > total_timeout_s:
            raise TimeoutError("stream total timeout exceeded")
        line = response.readline(MAX_SSE_LINE_BYTES + 1)
        if line in {b"", ""}:
            process_event()
            break
        line_bytes = line if isinstance(line, bytes) else line.encode("utf-8")
        if len(line_bytes) > MAX_SSE_LINE_BYTES:
            raise ResponseSizeError("SSE line exceeded the 4 MiB limit")
        total_bytes += len(line_bytes)
        if total_bytes > MAX_SSE_TOTAL_BYTES:
            raise ResponseSizeError("SSE response exceeded the 32 MiB limit")
        if isinstance(line, bytes):
            line = line.decode("utf-8")
        line = line.rstrip("\r\n")
        if not line:
            process_event()
            if saw_done:
                break
        elif line.startswith("data:"):
            data_lines.append(line[5:].lstrip())
            # [DONE] is a protocol sentinel, not JSON content.  Dispatch it
            # immediately so a server that keeps the connection alive after a
            # complete response cannot make us block on another readline.
            if len(data_lines) == 1 and data_lines[0].strip() == "[DONE]":
                process_event()
                break
        # SSE comments and unrelated fields are deliberately ignored.

    if not saw_event or not saw_done:
        raise StreamProtocolError("stream ended before a complete [DONE] sequence")
    if finish_reason != "stop":
        raise StreamProtocolError(
            "stream completed without the only accepted finish_reason=stop"
        )
    body = {
        "id": response_id,
        "model": response_model,
        "choices": [
            {
                "message": {"content": "".join(content)},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    return ChatResponse(body, http_status, reasoning_length)


class RequestAttempt:
    """One ledger-correlated request attempt, closed exactly once."""

    def __init__(
        self,
        *,
        stage: str,
        model: str,
        images: list[dict[str, Any]],
        prompt_sha256: str,
        context_sha256: str | None,
        options: RequestOptions,
        max_tokens: int,
        page: str | None = None,
        page_sha256: str | None = None,
        budget: RequestBudget | None = None,
        ledger: RequestLedger | None = None,
    ) -> None:
        self.ledger_id = uuid.uuid4().hex
        self._started = time.monotonic()
        self._closed = False
        self._ledger = ledger
        budget_snapshot = None
        if budget is not None:
            budget_snapshot = budget.reserve(
                image_attachments=len(images),
                estimated_cost_usd=options.estimated_cost_usd,
            )
        self._base = {
            "ledger_id": self.ledger_id,
            "stage": stage,
            "page": page,
            "page_sha256": page_sha256,
            "images": images,
            "image_count": len(images),
            "model": model,
            "prompt_sha256": prompt_sha256,
            "context_sha256": context_sha256,
            "timeout_s": options.timeout_s,
            "stream": options.stream,
            "stream_idle_timeout_s": options.stream_idle_timeout_s,
            "thinking_type_requested": (
                options.thinking["type"] if options.thinking is not None else "omitted"
            ),
            "reasoning_effort_requested": options.reasoning_effort,
            "max_tokens": max_tokens,
            "estimated_cost_usd": options.estimated_cost_usd,
            "budget_snapshot": budget_snapshot,
        }
        if self._ledger is not None:
            self._ledger.record(
                {
                    **self._base,
                    "event": "started",
                    "status": "started",
                    "result_status": "unknown",
                    "billing_status": "unknown",
                }
            )

    def _finish(self, fields: dict[str, Any]) -> None:
        if self._closed:
            return
        self._closed = True
        if self._ledger is not None:
            self._ledger.record(
                {
                    **self._base,
                    "event": "finished",
                    "elapsed_s": round(time.monotonic() - self._started, 6),
                    **fields,
                }
            )

    def success(
        self,
        *,
        http_status: int | None,
        response_id: Any,
        finish_reason: Any,
        usage: Any,
        reasoning_length: int,
    ) -> None:
        clean_usage = sanitize_usage(usage)
        self._finish(
            {
                "status": "succeeded",
                "result_status": "known",
                "billing_status": "known" if clean_usage else "unknown",
                "http_status": http_status,
                "response_id": str(response_id) if response_id is not None else None,
                "finish_reason": str(finish_reason) if finish_reason is not None else None,
                "usage": clean_usage,
                "reasoning_length": reasoning_length,
            }
        )

    def failed(
        self,
        *,
        error: BaseException,
        http_status: int | None = None,
        response_id: Any = None,
        finish_reason: Any = None,
        usage: Any = None,
        reasoning_length: int = 0,
        ambiguous: bool = False,
    ) -> None:
        clean_usage = sanitize_usage(usage)
        self._finish(
            {
                "status": "timeout_unknown" if ambiguous else "failed",
                "result_status": "unknown" if ambiguous else "failed",
                "billing_status": "unknown" if ambiguous or not clean_usage else "known",
                "http_status": http_status,
                "response_id": str(response_id) if response_id is not None else None,
                "finish_reason": str(finish_reason) if finish_reason is not None else None,
                "usage": clean_usage,
                "reasoning_length": reasoning_length,
                "error_type": type(error).__name__,
            }
        )
