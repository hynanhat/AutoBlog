"""Dọn các tệp ảnh Hiragana cục bộ khi người dùng bật tùy chọn."""

import os
import re
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from ninja_hiragana import OUTPUT_DIR
from ocr_client import FILENAME_REGEX


_CAPTURE_NAME = re.compile(r"hiragana-\d{8}-\d{6}-\d{6}\.png")


def cleanup_preference_file() -> Path:
    local_data = os.environ.get("LOCALAPPDATA") or Path.home() / ".config"
    return Path(local_data) / "AutoBlog" / "cleanup-ocr-on-exit.flag"


def save_cleanup_preference(enabled: bool) -> None:
    flag = cleanup_preference_file()
    if enabled:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.touch()
    else:
        flag.unlink(missing_ok=True)


def cleanup_ocr_images(project: Path) -> None:
    """Dọn ảnh chụp và ảnh đã xử lý trong hai thư mục mặc định của AutoBlog."""
    project = project.resolve()
    captures = project / OUTPUT_DIR
    if captures.is_dir() and captures.resolve() == captures:
        for path in captures.iterdir():
            if _CAPTURE_NAME.fullmatch(path.name):
                _remove_regular_file(path)

    inbox = project / "ocr" / "inbox"
    database = inbox / ".ocr-state" / "jobs.sqlite3"
    if not database.is_file() or database.resolve() != database:
        return

    from ocr.folder_ocr import file_stamp

    with closing(sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True, timeout=1)) as db:
        for path in inbox.iterdir():
            if not FILENAME_REGEX.fullmatch(path.name):
                continue
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            finished = db.execute(
                "SELECT 1 FROM jobs WHERE filename = ? AND stamp = ? AND status IN ('done', 'failed')",
                (path.name, file_stamp(info)),
            ).fetchone()
            if finished:
                _remove_regular_file(path, expected=info)


def _remove_regular_file(path: Path, *, expected: os.stat_result | None = None) -> None:
    try:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode) and (expected is None or info == expected):
            path.unlink(missing_ok=True)
    except FileNotFoundError:
        pass
