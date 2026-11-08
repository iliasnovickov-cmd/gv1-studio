"""Run the helper on a fixed loopback interface."""
from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the loopback-only GV1 helper")
    parser.add_argument("--port", type=int, default=8765, help="loopback TCP port (default: 8765)")
    parser.add_argument("--log-level", choices=("critical", "error", "warning", "info", "debug"), default="info")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    uvicorn.run(
        "gv1_helper.app:app",
        host="127.0.0.1",
        port=args.port,
        log_level=args.log_level,
        access_log=True,
        proxy_headers=False,
        server_header=False,
    )


if __name__ == "__main__":
    main()
