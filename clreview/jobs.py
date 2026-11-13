"""Background snapshot refresh: one thread per job, with progress and cancel.

A refresh of a large FOSSology folder outlives any sensible HTTP request, so the API starts it
here and the browser polls. Jobs live in memory only; the stored result is the snapshot a
finished refresh writes, exactly as the synchronous endpoint does.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from .connectors.base import RefreshCancelled
from .service import Service, now

KEEP = 50  # finished jobs remembered, oldest dropped first

log = logging.getLogger("clreview.jobs")


class RefreshJobs:
    def __init__(self, service: Service):
        self.service = service
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._next = 1

    def start(self, mapping_id: int, actor: str, full: bool = False) -> dict[str, Any]:
        self.service.mapping(mapping_id)  # 404 before a thread exists
        with self._lock:
            for job in self._jobs.values():
                if job["mapping_id"] == mapping_id and job["status"] == "running":
                    raise ValueError(f"a refresh of this mapping is already running (job {job['id']})")
            job_id = self._next
            self._next += 1
            job = {"id": job_id, "mapping_id": mapping_id, "status": "running", "full": full,
                   "progress": {"side": "", "done": 0, "total": None},
                   "result": None, "error": None, "started_at": now(), "finished_at": None}
            self._jobs[job_id] = job
            self._cancel[job_id] = threading.Event()
            for old in sorted(self._jobs)[:-KEEP]:
                if self._jobs[old]["status"] != "running":
                    self._jobs.pop(old)
                    self._cancel.pop(old, None)
        threading.Thread(target=self._run, args=(job_id, actor), daemon=True).start()
        return self.get(job_id)

    def get(self, job_id: int) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"refresh job {job_id} not found")
            job = self._jobs[job_id]
            return {**job, "progress": dict(job["progress"])}

    def cancel(self, job_id: int) -> dict[str, Any]:
        self.get(job_id)
        self._cancel[job_id].set()
        return self.get(job_id)

    def _progress(self, job_id: int, side: str, done: int, total: int | None) -> None:
        with self._lock:
            self._jobs[job_id]["progress"] = {"side": side, "done": done, "total": total}

    def _run(self, job_id: int, actor: str) -> None:
        job = self._jobs[job_id]
        outcome: dict[str, Any] = {}
        try:
            outcome = {"result": self.service.refresh(
                job["mapping_id"], actor, full=job["full"],
                on_progress=lambda side, done, total: self._progress(job_id, side, done, total),
                cancel=self._cancel[job_id]), "status": "done"}
        except RefreshCancelled:
            outcome = {"status": "cancelled"}
        except Exception as exc:  # the job reports it; the thread must not die silently
            log.exception("refresh job %s failed", job_id)
            outcome = {"status": "error", "error": str(exc) or type(exc).__name__}
        with self._lock:
            job.update(outcome)
            job["finished_at"] = now()
