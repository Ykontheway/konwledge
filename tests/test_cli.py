from __future__ import annotations

import pytest

from konwledge import __version__
from konwledge.cli import build_parser, main


def test_package_has_pre_alpha_version() -> None:
    assert __version__ == "0.0.0"


def test_parser_uses_project_name() -> None:
    assert build_parser().prog == "konwledge"


def test_cli_without_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "Manage versioned knowledge" in capsys.readouterr().out


def test_cli_reports_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])

    assert exc_info.value.code == 0
    assert capsys.readouterr().out.strip() == "konwledge 0.0.0"
