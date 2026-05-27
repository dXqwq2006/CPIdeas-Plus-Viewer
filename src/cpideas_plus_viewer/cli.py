from __future__ import annotations

import argparse
from pathlib import Path

from .server import serve_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cpideas-viewer",
        description="Start the read-only CPIdeas Plus product result viewer.",
    )
    parser.add_argument(
        "--runs-dir",
        default=Path("runs"),
        type=Path,
        help="Directory containing imported product results. Default: runs",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1"
    )
    parser.add_argument("--port", default=8765, type=int, help="Port to bind")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    serve_ui(
        runs_dir=args.runs_dir,
        host=args.host,
        port=args.port,
        actions_enabled=False,
        internal_files_enabled=False,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
