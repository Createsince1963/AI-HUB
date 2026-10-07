"""Resumable download manager (Qt threads). Files are written as <name>.part and renamed when complete."""
from __future__ import annotations

import itertools
import os
import shutil
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from .net import http_open

_ids = itertools.count(1)


@dataclass
class Job:
    label: str
    url: str
    dest: str
    headers: dict = field(default_factory=dict)
    size: int = 0                # expected total (0 = unknown)
    kind: str = "file"           # "file" | "ollama"
    id: int = field(default_factory=lambda: next(_ids))
    state: str = "queued"        # queued running done error cancelled
    done: int = 0
    speed: float = 0.0
    error: str = ""
    status: str = ""


class _FileWorker(QThread):
    progress = Signal(int)

    def __init__(self, job: Job):
        super().__init__()
        self.job = job
        self.cancel = False

    def run(self) -> None:
        j = self.job
        part = Path(j.dest + ".part")
        try:
            part.parent.mkdir(parents=True, exist_ok=True)
            have = part.stat().st_size if part.exists() else 0
            hdr = dict(j.headers)
            if have:
                hdr["Range"] = f"bytes={have}-"
            try:
                resp = http_open(j.url, hdr, timeout=30)
            except urllib.error.HTTPError as e:
                if e.code == 416 and have:          # already complete
                    os.replace(part, j.dest)
                    j.done = have
                    return
                raise
            with resp:
                status = getattr(resp, "status", 200)
                length = int(resp.headers.get("Content-Length") or 0)
                if status == 206:
                    total = have + length
                else:
                    have, total = 0, length
                j.size = total or j.size
                free = shutil.disk_usage(part.parent).free
                if j.size and j.size - have > free:
                    raise OSError(f"Not enough free space ({(j.size - have) / 1024 ** 3:.1f} GB needed, "
                                  f"{free / 1024 ** 3:.1f} GB free)")
                j.done = have
                t0, n0 = time.monotonic(), have
                with open(part, "ab" if status == 206 else "wb") as f:
                    while True:
                        if self.cancel:
                            j.state = "cancelled"
                            return
                        chunk = resp.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        j.done += len(chunk)
                        now = time.monotonic()
                        if now - t0 >= 0.5:
                            j.speed = (j.done - n0) / (now - t0)
                            t0, n0 = now, j.done
                            self.progress.emit(j.done)
            if j.size and j.done < j.size:
                raise OSError("Connection closed early - press Retry to resume")
            os.replace(part, j.dest)
        except Exception as exc:            # noqa: BLE001 - reported in the job row
            j.error = str(exc)
            j.state = "error"


class _OllamaWorker(QThread):
    progress = Signal(int)

    def __init__(self, job: Job, client):
        super().__init__()
        self.job, self.client, self.cancel = job, client, False

    def run(self) -> None:
        j = self.job
        try:
            def cb(status: str, done: int, total: int) -> None:
                j.status = status
                if total:
                    j.size, j.done = total, done
                self.progress.emit(j.done)
            self.client.pull(j.url, cb, lambda: self.cancel)
            if self.cancel:
                j.state = "cancelled"
        except Exception as exc:            # noqa: BLE001
            j.error = str(exc)
            j.state = "error"


class DownloadManager(QObject):
    changed = Signal(object)     # Job
    finished = Signal(object)    # Job (done / error / cancelled)

    def __init__(self, max_parallel: int = 2):
        super().__init__()
        self.jobs: list[Job] = []
        self.max_parallel = max_parallel
        self._workers: dict[int, QThread] = {}
        self._ollama_client = None

    def set_ollama(self, client) -> None:
        self._ollama_client = client

    def add_file(self, label: str, url: str, dest: str, headers: dict | None = None, size: int = 0) -> Job:
        for j in self.jobs:
            if j.dest == dest and j.state in ("queued", "running"):
                return j
        job = Job(label, url, dest, headers or {}, size)
        self.jobs.append(job)
        self.changed.emit(job)
        self._pump()
        return job

    def add_ollama(self, model: str) -> Job:
        job = Job(f"ollama pull {model}", model, "", kind="ollama")
        self.jobs.append(job)
        self.changed.emit(job)
        self._pump()
        return job

    def cancel(self, job: Job) -> None:
        w = self._workers.get(job.id)
        if w is not None:
            w.cancel = True
        elif job.state == "queued":
            job.state = "cancelled"
            self.changed.emit(job)

    def retry(self, job: Job) -> None:
        if job.state in ("error", "cancelled"):
            job.state, job.error, job.speed = "queued", "", 0.0
            self.changed.emit(job)
            self._pump()

    def clear_finished(self) -> None:
        self.jobs = [j for j in self.jobs if j.state in ("queued", "running")]

    def scan_existing(self, folder) -> list[Job]:
        """Adds a 'done' entry for every file already sitting in `folder` that this manager
        doesn't know about yet (downloaded outside the launcher, e.g. by browser or by hand, or
        left over from a previous run of the launcher). Only jobs started in THIS process are
        ever tracked automatically - without this, the Downloads tab shows nothing for anything
        it didn't download itself, even though the folder is not empty. In-progress .part files
        are skipped (they show up once finished/renamed, or via Retry if abandoned)."""
        folder = Path(folder)
        added: list[Job] = []
        if not folder.is_dir():
            return added
        known = {j.dest for j in self.jobs}
        for p in sorted(folder.iterdir()):
            if not p.is_file() or p.name.endswith(".part") or str(p) in known:
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            job = Job(label=p.name, url="", dest=str(p), size=size)
            job.done = size
            job.state = "done"
            self.jobs.append(job)
            self.changed.emit(job)
            added.append(job)
        return added

    def active(self) -> int:
        return sum(1 for j in self.jobs if j.state in ("queued", "running"))

    def _pump(self) -> None:
        running = sum(1 for j in self.jobs if j.state == "running")
        for job in self.jobs:
            if running >= self.max_parallel:
                break
            if job.state != "queued":
                continue
            if job.kind == "ollama" and self._ollama_client is None:
                job.state, job.error = "error", "Ollama client not configured"
                self.changed.emit(job)
                continue
            w = _OllamaWorker(job, self._ollama_client) if job.kind == "ollama" else _FileWorker(job)
            w.progress.connect(lambda _n, j=job: self.changed.emit(j))
            w.finished.connect(lambda j=job: self._done(j))
            self._workers[job.id] = w
            job.state = "running"
            self.changed.emit(job)
            w.start()
            running += 1

    def _done(self, job: Job) -> None:
        self._workers.pop(job.id, None)
        if job.state == "running":
            job.state = "done"
        self.changed.emit(job)
        self.finished.emit(job)
        self._pump()
