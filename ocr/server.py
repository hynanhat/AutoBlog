"""Theo dõi thư mục ảnh Hiragana và cung cấp kết quả qua HTTP."""

import argparse
import math
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import uvicorn
from dotenv import load_dotenv
from filelock import FileLock, Timeout
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from config import MAX_WORKERS, configured_model
from folder_ocr import FolderWatcher, JobStore
from ocr_service import OCRService
MAX_ID = (1 << 63) - 1


class UTF8JSONResponse(JSONResponse):
    # Windows PowerShell also needs the charset to display kana correctly.
    media_type = "application/json; charset=utf-8"


def create_app(inbox: Path, service, workers=MAX_WORKERS, *, interval=0.5):
    inbox = inbox.resolve()
    state = inbox / ".ocr-state"
    store = JobStore(state / "jobs.sqlite3")

    @asynccontextmanager
    async def lifespan(app):
        state.mkdir(parents=True, exist_ok=True)
        lock = FileLock(state / "watcher.lock", timeout=0)
        try:
            lock.acquire()
        except Timeout as exc:
            raise RuntimeError(f"Thư mục đã có dịch vụ OCR đang chạy: {inbox}") from exc
        try:
            with (
                service,
                FolderWatcher(inbox, store, service, workers, interval) as watcher,
            ):
                app.state.watcher = watcher
                yield
        finally:
            lock.release()

    def health(request):
        watcher = request.app.state.watcher
        healthy = watcher.thread.is_alive() and watcher.scan_error is None
        return UTF8JSONResponse(
            {
                "status": "ok" if healthy else "error",
                "inbox": str(inbox),
                "workers": workers,
                "model": service.model,
            },
            status_code=200 if healthy else 503,
        )

    def page(request):
        try:
            after = int(request.query_params.get("after", 0))
            limit = int(request.query_params.get("limit", 100))
            if not (0 <= after <= MAX_ID and 1 <= limit <= 100):
                raise ValueError()
        except ValueError:
            return UTF8JSONResponse(
                {"error": "after phải là số nguyên không âm, limit từ 1 đến 100."}, 400
            )
        completed = request.url.path == "/results"
        items = store.page(after, limit, completed=completed)
        field = "cursor" if completed else "id"
        return UTF8JSONResponse(
            {"items": items, "next_cursor": items[-1][field] if items else after}
        )

    def job(request):
        job_id = request.path_params["job_id"]
        item = store.get(job_id) if job_id <= MAX_ID else None
        if item is None:
            return UTF8JSONResponse({"error": "Không tìm thấy tác vụ."}, 404)
        return UTF8JSONResponse(item)

    return Starlette(
        lifespan=lifespan,
        routes=[
            Route("/health", health),
            Route("/jobs", page),
            Route("/jobs/{job_id:int}", job),
            Route("/results", page),
        ],
    )


def main(argv=None):
    load_dotenv(ROOT / ".env")
    if not os.getenv("GEMINI_API_KEY") and (ROOT.parent / ".env").is_file():
        load_dotenv(ROOT.parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inbox", type=Path, default=ROOT / "inbox")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument(
        "--workers", type=int, choices=range(1, MAX_WORKERS + 1), default=MAX_WORKERS
    )
    parser.add_argument("--model", default=configured_model())
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Giãn cách giữa các yêu cầu Gemini (giây).",
    )
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("--port phải từ 1 đến 65535.")
    if not math.isfinite(args.delay):
        parser.error("--delay phải là số hữu hạn.")
    try:
        service = OCRService(os.getenv("GEMINI_API_KEY", ""), args.model, args.delay)
    except ValueError as exc:
        parser.error(str(exc))
    uvicorn.run(
        create_app(args.inbox, service, args.workers), host=args.host, port=args.port
    )


if __name__ == "__main__":
    main()
