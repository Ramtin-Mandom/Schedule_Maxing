"""
The local web profile's launcher:

    python -m app.web [--data-dir DIR | --db-path FILE] [--timezone ZONE] [--port 8765]
                      [--backend-url URL] [--static-dir frontend/dist]

It opens (and migrates) this device's database -- the same file the desktop
app uses unless told otherwise -- and serves the local API on a loopback
address only. It prints a one-time link; open it in the browser to start the
session (the code travels in the URL fragment, which is never sent to a
server). Stop it with Ctrl+C: it waits for work in progress, then closes the
database. Run the desktop app and this service on the same database only one
at a time.
"""

from __future__ import annotations

import argparse
import ipaddress
import sys
from pathlib import Path

LOOPBACK_NAMES = {"localhost"}


def _loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.web", description="Schedule Maxing local web service.")
    location = parser.add_mutually_exclusive_group()
    location.add_argument("--db-path", type=Path, help="The SQLite database file (default: the desktop app's).")
    location.add_argument("--data-dir", type=Path, help="A data directory; its executions.db is used.")
    parser.add_argument("--timezone", help="IANA timezone to plan in (default: SCHEDULE_MAXING_TIMEZONE or UTC).")
    parser.add_argument("--host", default="127.0.0.1", help="A loopback address (default 127.0.0.1).")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--backend-url", help="Synchronize with this backend (saved for next time).")
    parser.add_argument("--static-dir", type=Path, help="The built web frontend to serve.")
    args = parser.parse_args(argv)

    if not _loopback(args.host):
        parser.error("the local service only listens on a loopback address (127.0.0.1, ::1 or localhost).")

    from config import settings  # imported late: --data-dir must be resolved before anything opens a database

    if args.data_dir is not None:
        db_path = args.data_dir / settings.EXECUTION_DB_FILENAME
    else:
        db_path = args.db_path or Path(settings.DATA_DIR) / settings.EXECUTION_DB_FILENAME

    import uvicorn

    from app.web.local_app import LocalWebConfig, create_local_app

    config = LocalWebConfig(
        db_path=db_path, timezone=args.timezone or settings.DEFAULT_TIMEZONE, host=args.host, port=args.port,
        backend_url=args.backend_url, static_dir=args.static_dir,
    )
    app = create_local_app(config)
    host = f"[{args.host}]" if ":" in args.host else args.host
    print(f"Schedule Maxing local service: database {db_path}")
    print(f"Open this link once to start your session: http://{host}:{args.port}/#bootstrap={config.bootstrap_code}")
    sys.stdout.flush()
    uvicorn.run(app, host=args.host, port=args.port, timeout_graceful_shutdown=int(config.shutdown_timeout))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
