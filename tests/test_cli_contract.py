from __future__ import annotations

from pathlib import Path

import pytest

from chinese_pdf_to_epub import cli


def test_build_parser_exposes_offline_rederive_contract():
    args = cli.build_parser().parse_args(
        ["build", "book-dir", "--title", "合成书", "--rederive-final"]
    )

    assert args.func is cli.cmd_build
    assert args.book_dir == Path("book-dir")
    assert args.rederive_final is True


@pytest.mark.parametrize("state", ["failed", "unsupported", "ambiguous"])
def test_quality_failure_states_require_review_exit(state: str):
    assert cli._has_quality_failures({state: 1}) is True


def test_blank_fallback_does_not_trigger_quality_failure_exit():
    assert cli._has_quality_failures({"ok": 2, "blank": 1}) is False


def test_argparse_usage_error_keeps_standard_exit_code_two():
    with pytest.raises(SystemExit) as raised:
        cli.build_parser().parse_args(["build", "book-dir"])

    assert raised.value.code == 2
