"""Command-line interface for Konwledge."""

from __future__ import annotations

import argparse
from collections.abc import Sequence

from konwledge import __version__


def build_parser() -> argparse.ArgumentParser:
    """Build the command-line argument parser."""
    parser = argparse.ArgumentParser(
        prog="konwledge",
        description="Manage versioned knowledge and evaluate retrieval changes.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the Konwledge command-line interface."""
    parser = build_parser()
    parser.parse_args(argv)
    parser.print_help()
    return 0
