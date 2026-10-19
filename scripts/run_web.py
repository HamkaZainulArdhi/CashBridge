"""Run the CashBridge web UI locally.

Usage:
    python scripts/run_web.py [--host 127.0.0.1] [--port 8000] [--db path]

Equivalent to: uvicorn app.main:create_app --factory --port 8000
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import uvicorn

from app.main import create_app


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--db", default=None, help="SQLite path (default: CASHBRIDGE_DB)")
    args = parser.parse_args(argv)

    uvicorn.run(create_app(args.db), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
