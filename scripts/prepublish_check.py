#!/usr/bin/env python3
"""发布前扫描工作树、打包产物和当前 Git 历史；只报告规则名，不回显秘密。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from chinese_pdf_to_epub.security_scan import (
    Finding,
    known_secret_values,
    scan_bytes,
    scan_tree,
)
from chinese_pdf_to_epub.subprocess_env import safe_subprocess_env

_DENIED_TRACKED_NAMES = {".env", "request-ledger.jsonl", "cost.json", "context.json"}
_DENIED_TRACKED_PARTS = {"books", "scans", "work", "output", "__pycache__", ".venv"}
_DENIED_TRACKED_SUFFIXES = {
    ".pdf", ".epub", ".key", ".pem", ".p12", ".pfx", ".jks", ".keystore", ".der"
}


def git_history_findings(root: Path) -> list[Finding]:
    if not (root / ".git").exists():
        return [Finding(".git", "git-history-unavailable")]
    findings: list[Finding] = []
    known = known_secret_values()
    objects = subprocess.run(
        ["git", "rev-list", "--objects", "--all"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        env=safe_subprocess_env(),
    ).stdout.splitlines()
    for line in objects:
        object_id, _, name = line.partition(" ")
        kind = subprocess.run(
            ["git", "cat-file", "-t", object_id],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            env=safe_subprocess_env(),
        ).stdout.strip()
        if kind not in {"blob", "commit"}:
            continue
        raw = subprocess.run(
            ["git", "cat-file", "-p", object_id],
            cwd=root,
            capture_output=True,
            check=True,
            env=safe_subprocess_env(),
        ).stdout
        label = f"git:{object_id[:12]}:{name or kind}"
        findings.extend(scan_bytes(label, raw, known_values=known))
    return findings


def git_tracked_path_findings(root: Path) -> list[Finding]:
    if not (root / ".git").exists():
        return [Finding(".git", "git-index-unavailable")]
    raw = subprocess.run(
        ["git", "ls-files", "-s", "-z"], cwd=root, capture_output=True, check=True,
        env=safe_subprocess_env(),
    ).stdout
    findings: list[Finding] = []
    for value in raw.split(b"\0"):
        if not value:
            continue
        header, separator, encoded_name = value.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3:
            findings.append(Finding("git-index", "unparseable-index-entry"))
            continue
        mode = fields[0]
        name = encoded_name.decode("utf-8", errors="replace")
        path = Path(name)
        folded_name = path.name.casefold()
        folded_parts = {part.casefold() for part in path.parts}
        if folded_name in {item.casefold() for item in _DENIED_TRACKED_NAMES} or (
            folded_parts & {item.casefold() for item in _DENIED_TRACKED_PARTS}
        ):
            findings.append(Finding(name, "denied-tracked-path"))
        if path.suffix.casefold() in _DENIED_TRACKED_SUFFIXES:
            findings.append(Finding(name, "denied-tracked-artifact"))
        if mode == b"120000":
            findings.append(Finding(name, "tracked-symlink-not-allowed"))
    return findings


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    root = Path(args[0] if args else ".").resolve()
    findings = scan_tree(root) + git_tracked_path_findings(root) + git_history_findings(root)
    unique = sorted(set(findings), key=lambda item: (item.path, item.rule))
    if unique:
        print(f"发布扫描失败：{len(unique)} 项")
        for item in unique:
            print(f"- {item.path}: {item.rule}")
        return 1
    print("发布扫描通过：工作树、归档内容和 Git 历史均无命中。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
