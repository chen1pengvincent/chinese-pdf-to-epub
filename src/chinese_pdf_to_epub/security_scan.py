"""面向仓库、发行包和 EPUB 的保守敏感信息扫描。"""

from __future__ import annotations

import os
import re
import tarfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DENIED_FILENAMES = {
    ".env",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "request-ledger.jsonl",
    "cost.json",
    "context.json",
    "scan-fingerprint.json",
    "credentials.json",
    "service-account.json",
    "id_rsa",
    "id_ed25519",
}
_DENIED_PARTS = {"work", "books", "scans", "output", ".venv", "__pycache__"}
_MAC_HOME_PREFIX = b"/" + b"Users/"
_MAX_ARCHIVE_MEMBERS = 10_000
_MAX_ARCHIVE_MEMBER_BYTES = 64 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 256 * 1024 * 1024

_TEXT_PATTERNS = {
    "private-key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "github-token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9_]{20,}\b"),
    "cloud-access-key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "sk-token": re.compile(rb"\bsk-(?:live-|test-)?[A-Za-z0-9_-]{20,}\b"),
    "pypi-token": re.compile(rb"\bpypi-[A-Za-z0-9_-]{30,}\b"),
    "npm-token": re.compile(rb"\bnpm_[A-Za-z0-9]{30,}\b"),
    "slack-token": re.compile(rb"\bxox[baprs]-[A-Za-z0-9-]{20,}\b"),
    "generic-secret-assignment": re.compile(
        rb"(?im)^[ \t]*(?:export[ \t]+)?['\"]?"
        rb"(?:[A-Za-z_][A-Za-z0-9_]*_)?"
        rb"(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)"
        rb"['\"]?[ \t]*[:=][ \t]*(?:['\"][^'\"\r\n]{12,}['\"]|"
        rb"[A-Za-z0-9_./+=:@-]{12,})[ \t]*(?:#[^\r\n]*)?$"
    ),
    "bearer-value": re.compile(rb"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"),
    "mac-home-path": re.compile(
        _MAC_HOME_PREFIX + rb"(?!example|runner|foo|name)[^/\s]+/"
    ),
}


@dataclass(frozen=True)
class Finding:
    path: str
    rule: str


def known_secret_values() -> list[bytes]:
    values: list[bytes] = []
    for name in ("OPENCODE_GO_API_KEY", "MX_APIKEY", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "")
        if len(value) >= 8:
            values.append(value.encode("utf-8"))
    return values


def scan_bytes(path: str, raw: bytes, *, known_values: Iterable[bytes] = ()) -> list[Finding]:
    findings = [Finding(path, rule) for rule, pattern in _TEXT_PATTERNS.items() if pattern.search(raw)]
    if any(value and value in raw for value in known_values):
        findings.append(Finding(path, "known-secret-value"))
    return findings


def _denied_path(path: Path) -> str | None:
    name = path.name.casefold()
    parts = {part.casefold() for part in path.parts}
    if name in {value.casefold() for value in _DENIED_FILENAMES} or path.suffix.lower() in {
        ".pdf", ".epub", ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".der"
    }:
        return "denied-artifact-path"
    if parts & {part.casefold() for part in _DENIED_PARTS}:
        return "denied-runtime-directory"
    if path.name.endswith(".budget-state.json"):
        return "denied-artifact-path"
    return None


def _unsafe_archive_member(name: str, *, is_dir: bool = False) -> bool:
    if not name or "\\" in name or "\x00" in name:
        return True
    # ZIP directory entries conventionally end in exactly one slash.  Ignore
    # that marker when validating path components, but retain empty components
    # anywhere else (including ``dir//``) as an unsafe ambiguous path.
    normalized = name[:-1] if is_dir and name.endswith("/") else name
    if not normalized:
        return True
    path = PurePosixPath(normalized)
    return path.is_absolute() or any(
        part in {"", ".", ".."} for part in normalized.split("/")
    )


def _looks_like_archive(raw: bytes) -> bool:
    return (
        raw.startswith((b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08", b"\x1f\x8b"))
        or (len(raw) > 262 and raw[257:262] == b"ustar")
    )


def scan_tree(root: Path, *, include_known_environment_secrets: bool = True) -> list[Finding]:
    root = root.resolve()
    known = known_secret_values() if include_known_environment_secrets else []
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(
            part in {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".ruff_cache"}
            for part in relative.parts
        ):
            continue
        if path.is_symlink():
            findings.append(Finding(relative.as_posix(), "symlink-not-allowed"))
            continue
        if not path.is_file():
            continue
        denied = _denied_path(relative)
        if denied:
            findings.append(Finding(relative.as_posix(), denied))
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            findings.append(Finding(relative.as_posix(), "unreadable-file"))
            continue
        findings.extend(scan_bytes(relative.as_posix(), raw, known_values=known))
        if zipfile.is_zipfile(path):
            try:
                with zipfile.ZipFile(path) as archive:
                    infos = archive.infolist()
                    names = [info.filename for info in infos]
                    if len(infos) > _MAX_ARCHIVE_MEMBERS:
                        findings.append(Finding(relative.as_posix(), "archive-member-limit"))
                        continue
                    if len(names) != len(set(names)):
                        findings.append(Finding(relative.as_posix(), "duplicate-archive-member"))
                    total_size = 0
                    for info in infos:
                        name = info.filename
                        if _unsafe_archive_member(name, is_dir=info.is_dir()):
                            findings.append(Finding(f"{relative.as_posix()}!{name}", "unsafe-archive-path"))
                            continue
                        mode = (info.external_attr >> 16) & 0o170000
                        if mode == 0o120000:
                            findings.append(Finding(f"{relative.as_posix()}!{name}", "archive-link-not-allowed"))
                            continue
                        member_path = Path(*PurePosixPath(name).parts)
                        denied_member = _denied_path(member_path)
                        if denied_member:
                            findings.append(Finding(f"{relative.as_posix()}!{name}", denied_member))
                            continue
                        if info.is_dir():
                            continue
                        total_size += info.file_size
                        if (
                            info.file_size > _MAX_ARCHIVE_MEMBER_BYTES
                            or total_size > _MAX_ARCHIVE_TOTAL_BYTES
                        ):
                            findings.append(Finding(f"{relative.as_posix()}!{name}", "archive-size-limit"))
                            continue
                        raw = archive.read(info)
                        if _looks_like_archive(raw):
                            findings.append(Finding(f"{relative.as_posix()}!{name}", "nested-archive-not-allowed"))
                            continue
                        findings.extend(
                            scan_bytes(
                                f"{relative.as_posix()}!{name}",
                                raw,
                                known_values=known,
                            )
                        )
            except (OSError, zipfile.BadZipFile, RuntimeError):
                findings.append(Finding(relative.as_posix(), "unreadable-archive"))
        elif tarfile.is_tarfile(path):
            try:
                with tarfile.open(path, "r:*") as archive:
                    members = archive.getmembers()
                    if len(members) > _MAX_ARCHIVE_MEMBERS:
                        findings.append(Finding(relative.as_posix(), "archive-member-limit"))
                        continue
                    names = [member.name for member in members]
                    if len(names) != len(set(names)):
                        findings.append(Finding(relative.as_posix(), "duplicate-archive-member"))
                    total_size = 0
                    for member in members:
                        if _unsafe_archive_member(member.name, is_dir=member.isdir()):
                            findings.append(Finding(f"{relative.as_posix()}!{member.name}", "unsafe-archive-path"))
                            continue
                        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
                            findings.append(Finding(f"{relative.as_posix()}!{member.name}", "archive-link-not-allowed"))
                            continue
                        if not member.isfile():
                            continue
                        member_path = Path(*PurePosixPath(member.name).parts)
                        denied_member = _denied_path(member_path)
                        label = f"{relative.as_posix()}!{member.name}"
                        if denied_member:
                            findings.append(Finding(label, denied_member))
                            continue
                        total_size += member.size
                        if member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                            findings.append(Finding(label, "archive-member-too-large"))
                            continue
                        if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                            findings.append(Finding(label, "archive-size-limit"))
                            continue
                        extracted = archive.extractfile(member)
                        if extracted is None:
                            findings.append(Finding(label, "unreadable-archive-member"))
                            continue
                        raw = extracted.read()
                        if _looks_like_archive(raw):
                            findings.append(Finding(label, "nested-archive-not-allowed"))
                            continue
                        findings.extend(scan_bytes(label, raw, known_values=known))
            except (OSError, tarfile.TarError, RuntimeError):
                findings.append(Finding(relative.as_posix(), "unreadable-archive"))
    return findings
