"""
Module kết nối dịch vụ OCR Hiragana cho AutoBlog.
Cung cấp Client giao tiếp HTTP, quản lý con trỏ (cursor) bền vững,
ghi ảnh nguyên tử (.part -> .png) vào thư mục inbox, và ánh xạ kết quả theo tệp.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from uuid import uuid4

LOG = logging.getLogger("ocr_client")

FILENAME_REGEX = re.compile(r"^ninja-acc-(\d+)-(\d+)-([a-f0-9]+)\.png$")


class OCRError(Exception):
    """Lỗi cơ sở cho các thao tác OCR."""
    pass


class OCRConnectionError(OCRError):
    """Lỗi kết nối hoặc HTTP 5xx từ dịch vụ OCR."""
    pass


class OCRValidationError(OCRError):
    """Lỗi nghiệp vụ khi kết quả không thỏa mãn điều kiện (ví dụ: độ dài != 6 ký tự)."""
    pass


class OCRJobFailedError(OCRError):
    """Lỗi khi OCR server trả về status = 'failed'."""
    pass


@dataclass
class OCRConfig:
    """Cấu hình kết nối dịch vụ OCR."""
    base_url: str = "http://127.0.0.1:8000"
    inbox_dir: Optional[Path] = None
    state_file: Path = Path("ocr_cursor.json")
    poll_interval: float = 1.0
    request_timeout: float = 10.0
    max_retries: int = 3
    backoff_factor: float = 1.0
    expected_length: int = 6
    auto_detect_inbox: bool = True

    def __post_init__(self):
        # Cho phép cấu hình qua biến môi trường OCR_HIRAGANA_URL
        env_url = os.getenv("OCR_HIRAGANA_URL")
        if env_url:
            self.base_url = env_url.strip()
        self.base_url = self.base_url.rstrip("/")
        if self.inbox_dir is not None:
            self.inbox_dir = Path(self.inbox_dir)
        self.state_file = Path(self.state_file)


class OCRStateStore:
    """
    Quản lý con trỏ (cursor) và danh sách job ID đã xử lý nhằm bảo đảm:
    - Lưu vị trí con trỏ bền vững trên đĩa.
    - Chỉ lưu next_cursor sau khi toàn bộ trang đã được tác vụ đích ghi nhận.
    - Đọc lại cùng id vẫn cho một kết quả nghiệp vụ duy nhất (idempotency).
    - Ghi nhận riêng các mục failed.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self.next_cursor: int = 0
        self.processed_ids: Set[int] = set()
        self.failed_jobs: List[Dict[str, Any]] = []
        self.load()

    def load(self) -> None:
        with self._lock:
            if not self.path.exists():
                self.next_cursor = 0
                self.processed_ids = set()
                self.failed_jobs = []
                return

            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.next_cursor = int(data.get("next_cursor", 0))
                self.processed_ids = set(data.get("processed_ids", []))
                self.failed_jobs = list(data.get("failed_jobs", []))
            except Exception as exc:
                LOG.warning("Không thể đọc tệp trạng thái OCR (%s): %s. Khởi tạo mới.", self.path, exc)
                self.next_cursor = 0
                self.processed_ids = set()
                self.failed_jobs = []

    def save(self) -> None:
        """Ghi nguyên tử bằng tệp tạm .tmp và os.replace."""
        with self._lock:
            self._save_unlocked()

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "next_cursor": self.next_cursor,
            "processed_ids": sorted(self.processed_ids),
            "failed_jobs": self.failed_jobs[-200:],  # Giữ tối đa 200 bản ghi lỗi gần nhất
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temp_path = self.path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temp_path.replace(self.path)

    def is_processed(self, job_id: int) -> bool:
        with self._lock:
            return job_id in self.processed_ids

    def mark_processed(self, job_id: int) -> None:
        with self._lock:
            self.processed_ids.add(job_id)

    def record_failure(self, job_id: int, filename: str, error: str) -> None:
        with self._lock:
            self.failed_jobs.append({
                "id": job_id,
                "filename": filename,
                "error": error,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            })

    def commit_page(self, new_processed_ids: List[int], next_cursor: int) -> None:
        """
        Lưu tiến độ sau khi toàn bộ trang đã được tác vụ đích xác nhận.
        Đảm bảo next_cursor và các job_id được cập nhật đồng thời.
        """
        with self._lock:
            for jid in new_processed_ids:
                self.processed_ids.add(jid)
            self.next_cursor = next_cursor
            self._save_unlocked()

    def reset(self, new_cursor: int = 0) -> None:
        """Đặt lại vị trí con trỏ và danh sách id đã xử lý khi máy chủ bị reset hoặc lùi mốc."""
        with self._lock:
            self.next_cursor = new_cursor
            self.processed_ids = set()
            self._save_unlocked()


class HiraganaOCRClient:
    """
    Client kết nối dịch vụ OCR Hiragana cục bộ.
    Tuân thủ đầy đủ tài liệu đặc tả:
    - Kiểm tra GET /health.
    - Lưu ảnh vào inbox bằng tên duy nhất (.part -> .png).
    - Đọc GET /results?after=N&limit=100.
    - Ánh xạ filename về đúng tác vụ gốc.
    - Dùng result.normalized_text cho mục 'done'.
    - Kiểm tra độ dài 6 ký tự cho nghiệp vụ Hiragana.
    - Ghi nhận riêng mục 'failed'.
    - Lưu tiến độ theo trang sau khi nhận dữ liệu, bảo đảm idempotency theo id.
    """

    def __init__(self, config: Optional[OCRConfig] = None):
        self.config = config or OCRConfig()
        self.state_store = OCRStateStore(self.config.state_file)
        self.inbox_dir = self.config.inbox_dir

        # Quản lý ánh xạ task đang chờ kết quả: filename -> asyncio.Future
        self._pending_futures: Dict[str, asyncio.Future] = {}
        # Ánh xạ filename -> thông tin ngữ cảnh (account_index, email)
        self._pending_meta: Dict[str, Dict[str, Any]] = {}

        self._polling_task: Optional[asyncio.Task] = None
        self._stop_requested: bool = False

    # -------------------------------------------------------------------------
    # HTTP REQUEST UTILS
    # -------------------------------------------------------------------------
    def _http_get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Gửi GET request đồng bộ và giải mã JSON UTF-8."""
        url = f"{self.config.base_url}{path}"
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}?{query}"

        req = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "AutoBlog-HiraganaClient/1.0",
            },
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.request_timeout) as resp:
                status_code = resp.getcode()
                raw_bytes = resp.read()
                data = json.loads(raw_bytes.decode("utf-8"))
                return data
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                raw_err = exc.read().decode("utf-8", errors="replace")
                raise ValueError(f"HTTP 400: {raw_err}") from exc
            if exc.code >= 500:
                raw_err = exc.read().decode("utf-8", errors="replace")
                raise OCRConnectionError(f"HTTP {exc.code} từ OCR server: {raw_err}") from exc
            raise OCRConnectionError(f"HTTP {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OCRConnectionError(f"Lỗi kết nối tới OCR server ({url}): {exc}") from exc

    # -------------------------------------------------------------------------
    # 1. HEALTH CHECK & INBOX RESOLUTION
    # -------------------------------------------------------------------------
    def check_health(self) -> Tuple[bool, Dict[str, Any]]:
        """
        Kiểm tra GET /health.
        HTTP 200 và status = 'ok' cho biết bộ theo dõi thư mục đang hoạt động.
        Đồng thời lấy đường dẫn thư mục inbox thực tế từ trường 'inbox'.
        """
        try:
            data = self._http_get("/health")
            is_ok = data.get("status") == "ok"
            if is_ok and self.config.auto_detect_inbox and not self.config.inbox_dir:
                remote_inbox = data.get("inbox")
                if remote_inbox:
                    self.inbox_dir = Path(remote_inbox)
            return is_ok, data
        except Exception as exc:
            return False, {"status": "error", "error": str(exc)}

    def resolve_inbox_dir(self) -> Path:
        """Xác định đường dẫn inbox thực tế (từ config, /health, hoặc thư mục mặc định)."""
        if self.inbox_dir:
            return self.inbox_dir

        # Thử lấy từ /health
        ok, data = self.check_health()
        if ok and data.get("inbox"):
            self.inbox_dir = Path(data["inbox"])
            return self.inbox_dir

        # Fallback thư mục ocr/inbox hoặc inbox ngay trong AutoBlog
        root_dir = Path(__file__).resolve().parent
        local_ocr_inbox = root_dir / "ocr" / "inbox"
        if local_ocr_inbox.parent.exists():
            self.inbox_dir = local_ocr_inbox
            return self.inbox_dir

        fallback_local = root_dir / "inbox"
        if fallback_local.exists():
            self.inbox_dir = fallback_local
            return self.inbox_dir

        # Fallback D:\OCR_Hiragana\inbox nếu có
        default_d = Path("D:/OCR_Hiragana/inbox")
        if default_d.parent.exists():
            self.inbox_dir = default_d
            return self.inbox_dir

        self.inbox_dir = local_ocr_inbox
        return self.inbox_dir

    # -------------------------------------------------------------------------
    # 2. TẠO TÊN DUY NHẤT & GHI ẢNH NGUYÊN TỬ (.part -> .png)
    # -------------------------------------------------------------------------
    @staticmethod
    def generate_filename(account_index: int, email: str = "") -> str:
        """
        Tạo tên tệp duy nhất gắn liền với STT tài khoản.
        Quy tắc: ninja-acc-{account_index}-{timestamp_ms}-{uuid_hex[:8]}.png
        """
        ts = int(time.time() * 1000)
        token = uuid4().hex[:8]
        return f"ninja-acc-{account_index}-{ts}-{token}.png"

    @staticmethod
    def parse_account_index(filename: str) -> Optional[int]:
        """Trích xuất account_index từ tên tệp nếu khớp quy tắc."""
        m = FILENAME_REGEX.match(filename)
        return int(m.group(1)) if m else None

    def submit_image(
        self,
        image_bytes: bytes,
        filename: str,
        inbox_dir: Optional[Path] = None,
    ) -> Path:
        """
        Ghi ảnh PNG/JPG vào thư mục inbox:
        - Ghi vào tệp tạm .part trước.
        - Đổi tên thành .png khi ghi xong để tránh OCR đọc tệp dở dang.
        """
        target_inbox = inbox_dir or self.resolve_inbox_dir()
        target_inbox.mkdir(parents=True, exist_ok=True)

        final_path = target_inbox / filename
        part_path = target_inbox / f"{filename}.part"

        with open(part_path, "wb") as f:
            f.write(image_bytes)
            f.flush()
            os.fsync(f.fileno())

        part_path.replace(final_path)
        return final_path

    # -------------------------------------------------------------------------
    # 3. LẤY VÀ XỬ LÝ TRANG KẾT QUẢ (/results)
    # -------------------------------------------------------------------------
    def fetch_results_page(self, after: int, limit: int = 100) -> Dict[str, Any]:
        """
        Gọi GET /results?after=N&limit=100.
        Trả về dict {"items": [...], "next_cursor": N}.
        """
        if after < 0 or not (1 <= limit <= 100):
            raise ValueError("after phải là số nguyên không âm, limit từ 1 đến 100.")
        return self._http_get("/results", {"after": after, "limit": limit})

    def process_page_items(
        self,
        page_data: Dict[str, Any],
        on_done: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_failed: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ) -> List[int]:
        """
        Xử lý từng mục trong một trang kết quả:
        1. Bỏ qua mục đã được xử lý trước đó (idempotency qua state_store.is_processed).
        2. Với done: lấy result.normalized_text, kiểm tra độ dài 6 ký tự.
        3. Với failed: ghi nhận riêng lỗi và chuyển cho callback.
        4. Trả về danh sách job_id vừa xử lý thành công trong trang này.
        """
        items = page_data.get("items", [])
        if not items:
            return []

        processed_in_this_page: List[int] = []

        for item in items:
            job_id = int(item["id"])
            filename = str(item.get("filename", ""))
            status = item.get("status")

            # Đảm bảo đọc lại cùng id vẫn cho một kết quả nghiệp vụ duy nhất
            if self.state_store.is_processed(job_id):
                LOG.debug("Job ID %s (%s) đã được xử lý trước đó, bỏ qua.", job_id, filename)
                continue

            if status == "done":
                res = item.get("result") or {}
                normalized_text = res.get("normalized_text", "")
                text = res.get("text", "")

                # Kiểm tra nghiệp vụ: Hiragana Ninja yêu cầu đúng 6 ký tự
                if len(normalized_text) == self.config.expected_length:
                    if on_done:
                        on_done(item, normalized_text)
                    self._dispatch_future_success(filename, normalized_text)
                else:
                    err_msg = (
                        f"Độ dài ký tự không hợp lệ ({len(normalized_text)} != {self.config.expected_length}): "
                        f"'{normalized_text}' (gốc: '{text}')"
                    )
                    LOG.warning("[%s] %s", filename, err_msg)
                    self.state_store.record_failure(job_id, filename, err_msg)
                    if on_failed:
                        on_failed(item, err_msg)
                    self._dispatch_future_failure(filename, OCRValidationError(err_msg))

            elif status == "failed":
                err_msg = item.get("error") or "Lỗi OCR không xác định"
                LOG.warning("[%s] OCR thất bại: %s", filename, err_msg)
                self.state_store.record_failure(job_id, filename, err_msg)
                if on_failed:
                    on_failed(item, err_msg)
                self._dispatch_future_failure(filename, OCRJobFailedError(err_msg))

            processed_in_this_page.append(job_id)

        # Lưu tiến độ next_cursor và các job_id chỉ sau khi toàn bộ trang đã được ghi nhận
        next_cursor = int(page_data.get("next_cursor", self.state_store.next_cursor))
        self.state_store.commit_page(processed_in_this_page, next_cursor)

        return processed_in_this_page

    def get_server_latest_cursor(self) -> Optional[int]:
        """
        Lấy mốc cursor lớn nhất hiện có trên máy chủ:
        1. Từ trường 'latest_cursor' trong /health (nếu server hỗ trợ).
        2. Nếu không có, gọi GET /results?after=0&limit=100 để xác định mốc lớn nhất.
        """
        try:
            ok, health_data = self.check_health()
            if ok and "latest_cursor" in health_data:
                return int(health_data["latest_cursor"])
        except Exception:
            pass

        try:
            page0 = self.fetch_results_page(after=0, limit=100)
            if "latest_cursor" in page0:
                return int(page0["latest_cursor"])
            items = page0.get("items", [])
            if not items:
                return 0
            if len(items) < 100:
                return int(items[-1].get("cursor", 0))
            return int(items[-1].get("cursor", 0))
        except Exception as exc:
            LOG.debug("Không thể lấy mốc cursor máy chủ: %s", exc)
            return None

    def sync_cursor(self) -> int:
        """
        Kiểm tra và tự động đồng bộ con trỏ với máy chủ nếu mốc đã lưu cao hơn mốc máy chủ.
        Trả về con trỏ hiện tại sau khi đồng bộ.
        """
        server_latest = self.get_server_latest_cursor()
        if server_latest is not None and self.state_store.next_cursor > server_latest:
            LOG.warning(
                "Mốc đọc đã lưu (%s) cao hơn mốc hiện có trên máy chủ OCR (%s). "
                "Tự động đặt lại con trỏ về 0 để không bỏ sót kết quả...",
                self.state_store.next_cursor,
                server_latest,
            )
            self.state_store.reset(0)
        return self.state_store.next_cursor

    def poll_once(
        self,
        limit: int = 100,
        on_done: Optional[Callable[[Dict[str, Any], str], None]] = None,
        on_failed: Optional[Callable[[Dict[str, Any], str], None]] = None,
    ) -> int:
        """
        Thực hiện một lần đọc trang từ con trỏ hiện tại:
        - Đọc GET /results?after=<con_trỏ_đã_lưu>&limit=100.
        - Tự động phát hiện khi con trỏ đã lưu > mốc hiện có của máy chủ
          (máy chủ bị reset hoặc cơ sở dữ liệu mới), đặt lại về 0 và đồng bộ ngay.
        - Nếu rỗng: giữ nguyên con trỏ.
        - Nếu có lỗi kết nối/5xx: giữ con trỏ và ném lỗi (cho caller retry).
        - Trả về số mục mới được xử lý.
        """
        current_cursor = self.state_store.next_cursor
        page_data = self.fetch_results_page(after=current_cursor, limit=limit)
        items = page_data.get("items", [])

        # 1. Kiểm tra trực tiếp từ trường latest_cursor do máy chủ trả về
        server_latest = page_data.get("latest_cursor")
        if server_latest is not None and current_cursor > int(server_latest):
            LOG.warning(
                "Mốc đọc đã lưu (%s) cao hơn mốc hiện có trên máy chủ (%s). Tự động đặt lại về 0...",
                current_cursor,
                server_latest,
            )
            self.state_store.reset(0)
            return self.poll_once(limit, on_done=on_done, on_failed=on_failed)

        # 2. Nếu không có items và cursor > 0, kiểm tra xem máy chủ có bị reset / lùi mốc không
        if not items and current_cursor > 0 and (self._pending_futures or server_latest is None):
            detected_latest = self.get_server_latest_cursor()
            if detected_latest is not None and current_cursor > detected_latest:
                LOG.warning(
                    "Phát hiện mốc đã lưu (%s) cao hơn mốc máy chủ (%s). Tự động đặt lại về 0...",
                    current_cursor,
                    detected_latest,
                )
                self.state_store.reset(0)
                return self.poll_once(limit, on_done=on_done, on_failed=on_failed)

        if not items:
            return 0
        processed = self.process_page_items(page_data, on_done=on_done, on_failed=on_failed)
        return len(processed)

    # -------------------------------------------------------------------------
    # 4. QUẢN LÝ TÁC VỤ BẤT ĐỒNG BỘ (ASYNCIO DISPATCH)
    # -------------------------------------------------------------------------
    @staticmethod
    def _safe_set_result(future: Any, val: str) -> None:
        if hasattr(future, "done") and not future.done():
            future.set_result(val)
        elif not hasattr(future, "done"):
            future.set_result(val)

    @staticmethod
    def _safe_set_exception(future: Any, exc: Exception) -> None:
        if hasattr(future, "done") and not future.done():
            future.set_exception(exc)
        elif not hasattr(future, "done"):
            future.set_exception(exc)

    def _dispatch_future_success(self, filename: str, result_text: str) -> None:
        future = self._pending_futures.pop(filename, None)
        self._pending_meta.pop(filename, None)
        if future and (not hasattr(future, "done") or not future.done()):
            try:
                loop = future.get_loop() if hasattr(future, "get_loop") else None
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._safe_set_result, future, result_text)
                else:
                    self._safe_set_result(future, result_text)
            except Exception:
                self._safe_set_result(future, result_text)

    def _dispatch_future_failure(self, filename: str, error: Exception) -> None:
        future = self._pending_futures.pop(filename, None)
        self._pending_meta.pop(filename, None)
        if future and (not hasattr(future, "done") or not future.done()):
            try:
                loop = future.get_loop() if hasattr(future, "get_loop") else None
                if loop is not None and loop.is_running():
                    loop.call_soon_threadsafe(self._safe_set_exception, future, error)
                else:
                    self._safe_set_exception(future, error)
            except Exception:
                self._safe_set_exception(future, error)

    async def solve_image(
        self,
        image_bytes: bytes,
        account_index: int,
        email: str,
        timeout: float = 60.0,
    ) -> Optional[str]:
        """
        Đầu vào chính cho Hook:
        1. Tạo tên tệp duy nhất.
        2. Đăng ký Future chờ kết quả.
        3. Ghi ảnh vào inbox (.part -> .png).
        4. Chờ kết quả từ vòng lặp đọc /results.
        """
        filename = self.generate_filename(account_index, email)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending_futures[filename] = fut
        self._pending_meta[filename] = {"account_index": account_index, "email": email}

        # Ghi ảnh vào inbox
        self.submit_image(image_bytes, filename)

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending_futures.pop(filename, None)
            self._pending_meta.pop(filename, None)

    # -------------------------------------------------------------------------
    # 5. VÒNG LẶP POLLING NỀN
    # -------------------------------------------------------------------------
    async def _polling_loop(self) -> None:
        """Vòng lặp đọc /results định kỳ."""
        LOG.info("Bắt đầu vòng lặp đọc kết quả OCR (cursor=%s)...", self.state_store.next_cursor)
        backoff = 1.0

        while not self._stop_requested:
            try:
                # Đọc trang kết quả
                loop = asyncio.get_running_loop()
                count = await loop.run_in_executor(None, self.poll_once, 100)
                if count > 0:
                    backoff = 1.0
                    # Nếu có dữ liệu, tiếp tục đọc ngay trang tiếp theo nếu còn
                    continue
                else:
                    # Trang rỗng: giữ con trỏ và chờ khoảng 1 giây
                    backoff = 1.0
                    await asyncio.sleep(self.config.poll_interval)
            except OCRConnectionError as exc:
                LOG.debug("Không thể kết nối dịch vụ OCR (sẽ thử lại sau %0.1fs): %s", backoff, exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)
            except Exception as exc:
                LOG.warning("Lỗi trong vòng lặp OCR: %s", exc)
                await asyncio.sleep(self.config.poll_interval)

    def start_polling(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Khởi động polling nền trong event loop."""
        try:
            self.sync_cursor()
        except Exception as exc:
            LOG.debug("Không thể đồng bộ con trỏ khi bắt đầu polling: %s", exc)

        if self._polling_task is None or self._polling_task.done():
            if loop is not None:
                target_loop = loop
            else:
                try:
                    target_loop = asyncio.get_running_loop()
                except RuntimeError:
                    target_loop = asyncio.get_event_loop()
            self._stop_requested = False
            self._polling_task = target_loop.create_task(self._polling_loop())

    async def stop_polling(self) -> None:
        """Dừng polling nền."""
        self._stop_requested = True
        if self._polling_task and not self._polling_task.done():
            self._polling_task.cancel()
            try:
                await self._polling_task
            except asyncio.CancelledError:
                pass
            self._polling_task = None


def ensure_ocr_server_running(
    base_url: str = "http://127.0.0.1:8000",
    ocr_dir: Optional[Path] = None,
    timeout: float = 8.0,
) -> Tuple[bool, str]:
    """
    Kiểm tra xem dịch vụ OCR có đang chạy không (/health).
    Nếu chưa chạy, tự động tìm thư mục ocr (ưu tiên ocr/ trong AutoBlog, sau đó D:\OCR_Hiragana)
    và khởi chạy tiến trình server.py ngầm.
    """
    health_url = f"{base_url.rstrip('/')}/health"

    def _is_healthy() -> bool:
        try:
            req = urllib.request.Request(health_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=1.5) as resp:
                return resp.getcode() == 200
        except Exception:
            return False

    if _is_healthy():
        return True, "Dịch vụ OCR đang hoạt động."

    root_dir = Path(__file__).resolve().parent
    if ocr_dir is None:
        local_ocr = root_dir / "ocr"
        if (local_ocr / "server.py").is_file():
            ocr_dir = local_ocr
        elif Path(r"D:\OCR_Hiragana\server.py").is_file():
            ocr_dir = Path(r"D:\OCR_Hiragana")
        else:
            ocr_dir = local_ocr
    else:
        ocr_dir = Path(ocr_dir)

    server_py = ocr_dir / "server.py"
    if not server_py.is_file():
        return False, f"Không tìm thấy dịch vụ OCR tại {ocr_dir} (thiếu server.py)."

    # Tìm Python executable có sẵn uvicorn & starlette
    import subprocess
    import sys

    python_candidates = [
        root_dir / ".venv" / "Scripts" / "python.exe",
        ocr_dir / ".venv" / "Scripts" / "python.exe",
        Path(r"D:\OCR_Hiragana\.venv\Scripts\python.exe"),
        Path(sys.executable),
    ]

    selected_python = None
    for cand in python_candidates:
        if cand.is_file():
            try:
                res = subprocess.run(
                    [str(cand), "-c", "import uvicorn; import starlette"],
                    capture_output=True,
                    timeout=2,
                )
                if res.returncode == 0:
                    selected_python = cand
                    break
            except Exception:
                continue

    if selected_python is None:
        for cand in python_candidates:
            if cand.is_file():
                selected_python = cand
                break

    if selected_python is None:
        return False, f"Không tìm thấy Python phù hợp để chạy OCR server tại {ocr_dir}."

    # Tự động khởi chạy tiến trình server.py
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        subprocess.Popen(
            [str(selected_python), str(server_py)],
            cwd=str(ocr_dir),
            creationflags=flags,
        )
    except Exception as exc:
        return False, f"Không thể tự khởi động OCR Server: {exc}"

    # Chờ server khởi động
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.5)
        if _is_healthy():
            return True, "Đã tự động khởi chạy OCR Server thành công."

    return False, "Đã gửi lệnh chạy OCR Server nhưng chưa nhận được phản hồi /health kịp thời."

