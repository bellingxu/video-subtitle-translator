"""启动入口：python -m subtitle_tool"""
from __future__ import annotations

import argparse

from . import config, server


def main() -> None:
    ap = argparse.ArgumentParser(description=config.APP_NAME)
    ap.add_argument("--port", type=int, default=config.DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true", help="只启动服务，不自动打开浏览器")
    args = ap.parse_args()
    server.serve(args.port, open_browser=not args.no_browser)


if __name__ == "__main__":
    main()
