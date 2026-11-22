"""Durable folder queue and concurrent OCR for the local HTTP service."""

import json
import logging
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from threading import Event, Thread

from formatter import normalize_hiragana
from preprocess import recognize_clean

LOG = logging.getLogger(__name__)
PUBLIC_FIELDS = "id, filename, status, cursor, created_at, finished_at, result, error"


class JobStore:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=15)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def initialize(self):
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY,
                    filename TEXT NOT NULL,
                    stamp TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'queued',
                    payload BLOB,
                    result TEXT,
                    error TEXT,
                    cursor INTEGER UNIQUE,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
                    finished_at TEXT,
                    UNIQUE(filename, stamp)
                );
                CREATE INDEX IF NOT EXISTS jobs_by_status ON jobs(status, id);
                UPDATE jobs SET status = 'queued' WHERE status = 'processing';
            """)

    def contains(self, name, stamp):
        with self.connect() as db:
            return (
                db.execute(
                    "SELECT 1 FROM jobs WHERE filename = ? AND stamp = ?", (name, stamp)
                ).fetchone()
                is not None
            )

    def enqueue(self, name, stamp, data):
        with self.connect() as db:
            db.execute(
                "INSERT INTO jobs(filename, stamp, payload) VALUES (?, ?, ?) "
                "ON CONFLICT(filename, stamp) DO NOTHING",
                (name, stamp, data),
            )

    def claim(self, count):
        with self.connect() as db:
            rows = db.execute(
                "SELECT id, payload FROM jobs WHERE status = 'queued' ORDER BY id LIMIT ?",
                (count,),
            ).fetchall()
            db.executemany(
                "UPDATE jobs SET status = 'processing' WHERE id = ?",
                [(row["id"],) for row in rows],
            )
            return rows

    def finish(self, job_id, result, error):
        with self.connect() as db:
            db.execute(
                """
                UPDATE jobs SET status = ?, result = ?, error = ?, payload = NULL,
                    cursor = (SELECT COALESCE(MAX(cursor), 0) + 1 FROM jobs),
                    finished_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?
            """,
                (
                    "failed" if error else "done",
                    json.dumps(result, ensure_ascii=False)
                    if result is not None
                    else None,
                    error,
                    job_id,
                ),
            )

    @staticmethod
    def public(row):
        item = dict(row)
        if item["result"] is not None:
            item["result"] = json.loads(item["result"])
        return item

    def page(self, after, limit, *, completed):
        # A slow, early job must still appear after a consumer advances its cursor.
        column = "cursor" if completed else "id"
        with self.connect() as db:
            rows = db.execute(
                f"SELECT {PUBLIC_FIELDS} FROM jobs WHERE {column} > ? ORDER BY {column} LIMIT ?",
                (after, limit),
            )
            return [self.public(row) for row in rows]

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute(
                f"SELECT {PUBLIC_FIELDS} FROM jobs WHERE id = ?", (job_id,)
            ).fetchone()
            return self.public(row) if row is not None else None

    def max_cursor(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(MAX(cursor), 0) FROM jobs").fetchone()
            return int(row[0]) if row and row[0] is not None else 0

    def max_id(self) -> int:
        with self.connect() as db:
            row = db.execute("SELECT COALESCE(MAX(id), 0) FROM jobs").fetchone()
            return int(row[0]) if row and row[0] is not None else 0


def file_stamp(info):
    return f"{info.st_dev}/{info.st_ino}/{info.st_mtime_ns}/{info.st_ctime_ns}/{info.st_size}"


class FolderWatcher:
    def __init__(self, inbox, store, service, workers, interval=0.5):
        self.inbox = inbox
        self.store = store
        self.service = service
        self.workers = workers
        self.interval = interval
        self.stop = Event()
        self.thread = Thread(target=self.run, name="ocr-folder")
        self.previous = {}
        self.ingested = {}
        self.scan_error = None

    def __enter__(self):
        self.store.initialize()
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.stop.set()
        self.thread.join()

    def scan(self):
        current = {}
        for path in self.inbox.iterdir():
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg"}:
                continue
            try:
                info = path.stat(follow_symlinks=False)
                if not stat.S_ISREG(info.st_mode):
                    continue
                name, stamp = path.name, file_stamp(info)
                current[name] = stamp
                if self.previous.get(name) != stamp or self.ingested.get(name) == stamp:
                    continue
                if not self.store.contains(name, stamp):
                    data = path.read_bytes()
                    if len(data) != info.st_size or file_stamp(path.stat()) != stamp:
                        continue
                    self.store.enqueue(name, stamp, data)
                self.ingested[name] = stamp
            except (FileNotFoundError, PermissionError):
                # A producer can rename the file or hold a Windows write lock.
                continue
        self.previous = current
        self.ingested = {n: s for n, s in self.ingested.items() if n in current}

    def process(self, data):
        result = recognize_clean(data, self.service)
        return {
            **result,
            "normalized_text": normalize_hiragana(result["text"]),
            "model": self.service.model,
        }

    def collect(self, pending, *, drain=False):
        for future, job_id in list(pending.items()):
            if not drain and not future.done():
                continue
            try:
                result = future.result()
            except (ValueError, RuntimeError) as exc:
                self.store.finish(job_id, None, str(exc) or "Lỗi OCR.")
            except Exception as exc:  # noqa: BLE001 - isolate a failed image in the batch
                # Other SDK/transport exceptions can contain request details.
                self.store.finish(
                    job_id, None, f"Lỗi xử lý ảnh ({type(exc).__name__})."
                )
            else:
                self.store.finish(job_id, result, None)
            del pending[future]

    def run(self):
        pending = {}
        with ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="ocr"
        ) as pool:
            while not self.stop.is_set():
                self.collect(pending)
                try:
                    self.scan()
                    self.scan_error = None
                except OSError as exc:
                    if self.scan_error != str(exc):
                        LOG.warning("Không quét được thư mục: %s", exc)
                    self.scan_error = str(exc)
                capacity = self.workers - len(pending)
                if capacity:
                    for row in self.store.claim(capacity):
                        pending[pool.submit(self.process, row["payload"])] = row["id"]
                self.stop.wait(self.interval)
            self.collect(pending, drain=True)
