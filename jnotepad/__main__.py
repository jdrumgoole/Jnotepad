"""``python -m jnotepad`` / ``jnotepad`` — run the app with uvicorn."""

from __future__ import annotations

import argparse
import logging


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Jnotepad web app.")
    parser.add_argument("--host", default="127.0.0.1",
                        help="Bind address (default 127.0.0.1: local only)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    import uvicorn

    from jnotepad.main import create_app

    logging.basicConfig(level=logging.INFO)
    # One worker only: the embedded SecantusDB server holds an exclusive
    # WiredTiger lock on the data directory.
    uvicorn.run(create_app(), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
