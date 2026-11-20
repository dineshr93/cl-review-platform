"""Background snapshot refresh: one thread per job, with progress and cancel.

A refresh of a large FOSSology folder outlives any sensible HTTP request, so the API starts it
here and the browser polls. Jobs live in memory only; the stored result is the snapshot a
finished refresh writes, exactly as the synchronous endpoint does.

Status runs ``running`` -> ``done`` | ``error`` | ``cancelled``. A cancel request reports
``cancelling`` at once and ``cancelled`` when the thread has stopped; nothing from a cancelled
refresh is stored (see ``Service.refresh``). Once the job reaches its ``commit`` phase it is
saving one transaction and a cancel is refused, because stopping it then could not undo more
than letting it finish.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .connectors.base import RefreshCancelled
from .service import Service, now

KEEP = 50  # finished jobs remembered, oldest dropped first
ACTIVE = ("running", "cancelling")

log = logging.getLogger("clreview.jobs")


class RefreshJobs:
    def __init__(self, service: Service):
        self.service = service
        self._lock = threading.Lock()
        self._jobs: dict[int, dict[str, Any]] = {}
        self._cancel: dict[int, threading.Event] = {}
        self._clock: dict[int, dict[str, float]] = {}  # monotonic start of the job and of its current side
        self._next = 1

    def start(self, mapping_id: int, actor: str, full: bool = False,
              estimate_seconds: float | None = None) -> dict[str, Any]:
        self.service.mapping(mapping_id)  # 404 before a thread exists
        with self._lock:
            for job in self._jobs.values():
                if job["mapping_id"] == mapping_id and job["status"] in ACTIVE:
                    raise ValueError(f"a refresh of this mapping is already running (job {job['id']})")
            job_id = self._next
            self._next += 1
            job = {"id": job_id, "mapping_id": mapping_id, "status": "running", "full": full,
                   "progress": {"side": "", "done": 0, "total": None},
                   "estimate_seconds": estimate_seconds,
                   "result": None, "error": None, "started_at": now(), "finished_at": None}
            self._jobs[job_id] = job
            self._cancel[job_id] = threading.Event()
            self._clock[job_id] = {"job": time.monotonic(), "side": time.monotonic()}
            for old in sorted(self._jobs)[:-KEEP]:
                if self._jobs[old]["status"] not in ACTIVE:
                    self._jobs.pop(old)
                    self._cancel.pop(old, None)
                    self._clock.pop(old, None)
        threading.Thread(target=self._run, args=(job_id, actor), daemon=True).start()
        return self.get(job_id)

    def get(self, job_id: int) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"refresh job {job_id} not found")
            job = self._jobs[job_id]
            out = {**job, "progress": dict(job["progress"])}
            if job["status"] in ACTIVE:
                clock, t = self._clock[job_id], time.monotonic()
                out["elapsed_seconds"] = round(t - clock["job"], 1)
                out["progress"]["side_elapsed_seconds"] = round(t - clock["side"], 1)
            return out

    def running_for(self, mapping_id: int) -> int | None:
        with self._lock:
            return next((j["id"] for j in self._jobs.values()
                         if j["mapping_id"] == mapping_id and j["status"] in ACTIVE), None)

    def cancel(self, job_id: int) -> dict[str, Any]:
        with self._lock:
            if job_id not in self._jobs:
                raise KeyError(f"refresh job {job_id} not found")
            job = self._jobs[job_id]
            if job["status"] == "running":
                if job["progress"]["side"] == "commit":
                    raise ValueError("the refresh is saving its results in one transaction and can no "
                                     "longer be cancelled; it finishes in a moment")
                job["status"] = "cancelling"
                self._cancel[job_id].set()
        return self.get(job_id)

    def _progress(self, job_id: int, side: str, done: int, total: int | None) -> None:
        with self._lock:
            job = self._jobs[job_id]
            if job["progress"]["side"] != side:
                self._clock[job_id]["side"] = time.monotonic()
            job["progress"] = {"side": side, "done": done, "total": total}

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
