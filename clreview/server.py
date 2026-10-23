"""CLI entry point: `python3 -m clreview.server --demo`."""
from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading

from .api import serve
from .db import DB
from .service import Service
from . import __version__

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "demo")


def seed_demo(db: DB, fixtures_dir: str = FIXTURES) -> dict:
    s = Service(db, fixtures_dir=fixtures_dir)
    if s.list_connections():
        return {"seeded": False, "reason": "connections already exist"}
    pl = s.add_connection("pl", "mock-pl", "Demo Black Duck (project-level)", "", {"fixtures": os.path.join(fixtures_dir, "pl.json")}, {})
    sl = s.add_connection("sl", "mock-sl", "Demo FOSSology (source-level)", "", {"fixtures": os.path.join(fixtures_dir, "sl.json")}, {})
    s.test_connection(pl["id"], "demo")
    s.test_connection(sl["id"], "demo")
    mapping = s.create_mapping("demo-app 1.0.0 / demo-scan", pl["id"], "demo-app", "1.0.0", sl["id"], "demo-scan", "demo")
    result = s.refresh(mapping["id"], "demo")
    return {"seeded": True, "mapping_id": mapping["id"], "refresh": result}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser("clreview-server")
    parser.add_argument("--db", default=os.environ.get("CLREVIEW_DB", "clreview.db"),
                        help="SQLite database path (default: $CLREVIEW_DB or clreview.db)")
    parser.add_argument("--host", default=os.environ.get("CLREVIEW_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("CLREVIEW_PORT", "8765")))
    parser.add_argument("--demo", action="store_true", help="seed mock PL/SL connections and demo data")
    parser.add_argument("--fixtures", default=FIXTURES)
    parser.add_argument("--log-level", default=os.environ.get("CLREVIEW_LOG_LEVEL", "info"),
                        choices=["debug", "info", "warning", "error"])
    parser.add_argument("--version", action="version", version=f"clreview-server {__version__}")
    args = parser.parse_args(argv)

    logging.basicConfig(level=getattr(logging, args.log_level.upper()),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    log = logging.getLogger("clreview")

    db = DB(args.db)
    if args.demo:
        info = seed_demo(db, args.fixtures)
        print(f"demo: {info}")
    token = os.environ.get("CLREVIEW_TOKEN")
    if args.host not in ("127.0.0.1", "localhost", "::1") and not token:
        log.warning("binding to %s without CLREVIEW_TOKEN: the API is unauthenticated", args.host)
    httpd = serve(db, host=args.host, port=args.port, fixtures_dir=args.fixtures, auth_token=token)
    log.info("CL Review %s listening on http://%s:%s (db=%s, auth=%s)",
             __version__, args.host, args.port, args.db, "on" if token else "off")

    stopping = threading.Event()

    def _stop(signum: int, _frame) -> None:
        if stopping.is_set():
            return
        stopping.set()
        log.info("received signal %s, shutting down", signum)
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, _stop)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log.info("stopped")
    finally:
        httpd.server_close()
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
