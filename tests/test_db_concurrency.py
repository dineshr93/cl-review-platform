"""The HTTP server is threaded and hands every request the same sqlite connection."""
from __future__ import annotations

import threading

from clreview.db import DB


def test_concurrent_statements_are_serialized():
    db = DB(":memory:")
    errors: list[BaseException] = []

    def worker(actor: str) -> None:
        try:
            for i in range(40):
                db.execute("INSERT INTO audit(at,actor,action) VALUES('now',?,?)", (actor, f"e{i}"))
                row = db.one("SELECT action FROM audit WHERE actor=? ORDER BY id DESC LIMIT 1", (actor,))
                assert row and row["action"] == f"e{i}"
                db.query("SELECT id FROM audit LIMIT 10")
        except BaseException as exc:  # collected so the assertion below names the cause
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"t{n}",)) for n in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent sqlite access raised: {errors[:3]}"
    assert len(db.query("SELECT id FROM audit")) == 8 * 40
