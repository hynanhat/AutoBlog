"""
Bộ kiểm thử tích hợp và kiểm thử đơn vị cho module OCR Hiragana.
Bao gồm các kịch bản bắt buộc theo tài liệu đặc tả:
1. Một trang rỗng (empty page)
2. Một ảnh lỗi (failed image)
3. Các ảnh hoàn tất khác thứ tự gửi (out-of-order completion)
4. Một lần khởi động lại giữa trang (restart midway through page - idempotency & cursor persistence)
Cùng các kiểm thử bổ sung:
- Ghi tệp nguyên tử (.part -> .png)
- Kiểm tra độ dài 6 ký tự
- Cơ chế Fallback sang thủ công khi OCR thất bại
- Xử lý lỗi HTTP 500 / URLError và giữ con trỏ
"""

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import patch
import urllib.error
import urllib.parse

from ocr_client import (
    HiraganaOCRClient,
    OCRConfig,
    OCRConnectionError,
    OCRJobFailedError,
    OCRStateStore,
    OCRValidationError,
)
from hiragana_hook import ManualHiraganaHook, OCRHiraganaHook


class MockHTTPResponse:
    """Giả lập đối tượng response từ urllib.request.urlopen."""

    def __init__(self, data: Dict[str, Any], status: int = 200):
        self._raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        pass


class MockFuture:
    """Mock Future độc lập không phụ thuộc socketpair mạng của event loop."""

    def __init__(self):
        self._done = False
        self._result = None
        self._exception = None

    def done(self) -> bool:
        return self._done

    def result(self):
        if self._exception:
            raise self._exception
        return self._result

    def set_result(self, val):
        self._done = True
        self._result = val

    def set_exception(self, exc):
        self._done = True
        self._exception = exc


def run_coro(coro):
    """Thực thi coroutine thuần túy không cần socketpair mạng."""
    try:
        coro.send(None)
    except StopIteration as e:
        return e.value


class TestOCRHiraganaIntegration(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(tempfile.mkdtemp(prefix="autoblog_ocr_test_"))
        self.state_file = self.test_dir / "ocr_cursor.json"
        self.inbox_dir = self.test_dir / "inbox"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)

        self.mock_server_data: Dict[str, Any] = {
            "status": "ok",
            "inbox": str(self.inbox_dir),
            "model": "gemini-2.5-flash",
            "results": [],
        }

        # Mock urllib.request.urlopen để chạy độc lập và tương thích mọi môi trường sandbox
        self.urlopen_patcher = patch("urllib.request.urlopen", side_effect=self._mock_urlopen)
        self.mock_urlopen = self.urlopen_patcher.start()

    def tearDown(self):
        self.urlopen_patcher.stop()
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def _mock_urlopen(self, req, timeout=None):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        parsed = urllib.parse.urlparse(url)
        query = urllib.parse.parse_qs(parsed.query)
        path = parsed.path

        if path == "/health":
            return MockHTTPResponse({
                "status": self.mock_server_data.get("status", "ok"),
                "inbox": self.mock_server_data.get("inbox", ""),
                "model": self.mock_server_data.get("model", "gemini-2.5-flash"),
            })

        elif path == "/results":
            try:
                after = int(query.get("after", [0])[0])
                limit = int(query.get("limit", [100])[0])
                if after < 0 or not (1 <= limit <= 100):
                    fp = io.BytesIO(b'{"error": "after phai la so nguyen khong am, limit tu 1 den 100."}')
                    raise urllib.error.HTTPError(url, 400, "Bad Request", {}, fp)
            except ValueError:
                fp = io.BytesIO(b'{"error": "after phai la so nguyen khong am, limit tu 1 den 100."}')
                raise urllib.error.HTTPError(url, 400, "Bad Request", {}, fp)

            all_results = self.mock_server_data.get("results", [])
            # Lọc các items có cursor > after
            matched = [item for item in all_results if item.get("cursor", 0) > after]
            paged = matched[:limit]
            next_cursor = paged[-1]["cursor"] if paged else after
            return MockHTTPResponse({"items": paged, "next_cursor": next_cursor})

        elif path == "/error500":
            fp = io.BytesIO(b'{"error": "Internal Server Error"}')
            raise urllib.error.HTTPError(url, 500, "Internal Server Error", {}, fp)

        fp = io.BytesIO(b'{"error": "Not Found"}')
        raise urllib.error.HTTPError(url, 404, "Not Found", {}, fp)

    # -------------------------------------------------------------------------
    # KỊCH BẢN BẮT BUỘC 1: MỘT TRANG RỖNG (EMPTY PAGE)
    # -------------------------------------------------------------------------
    def test_empty_page(self):
        """
        Kiểm thử khi OCR chưa có kết quả nào (items: []).
        Ứng dụng phải giữ nguyên con trỏ, không phát sinh lỗi, không ghi nhận bừa.
        """
        self.mock_server_data["results"] = []

        config = OCRConfig(
            base_url="http://127.0.0.1:8000",
            inbox_dir=self.inbox_dir,
            state_file=self.state_file,
        )
        client = HiraganaOCRClient(config)

        # Ban đầu cursor = 0
        self.assertEqual(client.state_store.next_cursor, 0)

        # Đọc trang rỗng
        processed_count = client.poll_once()
        self.assertEqual(processed_count, 0)

        # Con trỏ vẫn giữ nguyên 0
        self.assertEqual(client.state_store.next_cursor, 0)
        self.assertEqual(len(client.state_store.processed_ids), 0)

    # -------------------------------------------------------------------------
    # KỊCH BẢN BẮT BUỘC 2: MỘT ẢNH LỖI (FAILED IMAGE)
    # -------------------------------------------------------------------------
    def test_failed_image(self):
        """
        Kiểm thử khi có một ảnh bị lỗi (status = 'failed'):
        - Ghi nhận riêng id, filename, error vào failed_jobs.
        - Đánh dấu job_id đã xử lý để không lặp lại.
        - Chuyển con trỏ tiếp theo an toàn.
        - Không làm sập hoặc ảnh hưởng các tác vụ còn lại.
        """
        self.mock_server_data["results"] = [
            {
                "id": 10,
                "cursor": 8,
                "filename": "ninja-acc-1-12345.png",
                "status": "failed",
                "result": None,
                "error": "Tệp rỗng hoặc không phải ảnh PNG/JPEG đọc được.",
            }
        ]

        config = OCRConfig(
            base_url="http://127.0.0.1:8000",
            inbox_dir=self.inbox_dir,
            state_file=self.state_file,
        )
        client = HiraganaOCRClient(config)

        failed_reported = []

        def on_failed(item, err):
            failed_reported.append((item["filename"], err))

        processed_count = client.poll_once(on_failed=on_failed)
        self.assertEqual(processed_count, 1)

        # Kiểm tra ghi nhận riêng trong state_store
        self.assertEqual(len(client.state_store.failed_jobs), 1)
        failure_record = client.state_store.failed_jobs[0]
        self.assertEqual(failure_record["id"], 10)
        self.assertEqual(failure_record["filename"], "ninja-acc-1-12345.png")
        self.assertIn("không phải ảnh PNG/JPEG", failure_record["error"])

        # Callback nhận đúng thông tin lỗi
        self.assertEqual(len(failed_reported), 1)
        self.assertEqual(failed_reported[0][0], "ninja-acc-1-12345.png")

        # Con trỏ đã tiến tới 8
        self.assertEqual(client.state_store.next_cursor, 8)
        self.assertTrue(client.state_store.is_processed(10))

    # -------------------------------------------------------------------------
    # KỊCH BẢN BẮT BUỘC 3: CÁC ẢNH HOÀN TẤT KHÁC THỨ TỰ GỬI (OUT-OF-ORDER)
    # -------------------------------------------------------------------------
    def test_out_of_order_completion(self):
        """
        Kiểm thử khi gửi ảnh tài khoản 1 rồi tài khoản 2, nhưng OCR xử lý xong
        tài khoản 2 trước tài khoản 1:
        - Dùng filename để ghép chữ đã chuẩn hóa vào đúng tác vụ gốc.
        - Cả 2 tài khoản đều nhận đúng kết quả của mình bất kể thứ tự hoàn tất.
        """
        filename_acc1 = "ninja-acc-1-1000-aaa.png"
        filename_acc2 = "ninja-acc-2-2000-bbb.png"

        # Giả lập: acc 2 hoàn tất ở cursor 1, acc 1 hoàn tất sau ở cursor 2
        self.mock_server_data["results"] = [
            {
                "id": 2,
                "cursor": 1,
                "filename": filename_acc2,
                "status": "done",
                "result": {
                    "text": "かきくけこさ",
                    "normalized_text": "かきくけこさ",
                    "warnings": [],
                },
                "error": None,
            },
            {
                "id": 1,
                "cursor": 2,
                "filename": filename_acc1,
                "status": "done",
                "result": {
                    "text": "はひじぬせさ",
                    "normalized_text": "はひしぬせさ",
                    "warnings": [],
                },
                "error": None,
            },
        ]

        config = OCRConfig(
            base_url="http://127.0.0.1:8000",
            inbox_dir=self.inbox_dir,
            state_file=self.state_file,
        )
        client = HiraganaOCRClient(config)

        # Giả lập 2 Future đang chờ tương ứng (độc lập mạng)
        fut1 = MockFuture()
        fut2 = MockFuture()
        client._pending_futures[filename_acc1] = fut1
        client._pending_futures[filename_acc2] = fut2

        # Xử lý trang
        processed_count = client.poll_once()
        self.assertEqual(processed_count, 2)

        # Kiểm tra kết quả ánh xạ chính xác về đúng task
        self.assertTrue(fut1.done())
        self.assertTrue(fut2.done())
        self.assertEqual(fut1.result(), "はひしぬせさ")  # Đúng acc 1
        self.assertEqual(fut2.result(), "かきくけこさ")  # Đúng acc 2

        # Con trỏ tiến tới cursor lớn nhất (2)
        self.assertEqual(client.state_store.next_cursor, 2)
        self.assertTrue(client.state_store.is_processed(1))
        self.assertTrue(client.state_store.is_processed(2))

    # -------------------------------------------------------------------------
    # KỊCH BẢN BẮT BUỘC 4: KHỞI ĐỘNG LẠI GIỮA TRANG (RESTART MIDWAY & IDEMPOTENCY)
    # -------------------------------------------------------------------------
    def test_restart_midway_through_page(self):
        """
        Kiểm thử khởi động lại giữa trang:
        - Một trang gồm 3 mục [A, B, C].
        - Xử lý mục A thành công nhưng ứng dụng bị dừng đột ngột (crash/restart) trước khi lưu next_cursor.
        - Khi khởi động lại, con trỏ trên đĩa vẫn là con trỏ cũ, client đọc lại cả 3 mục [A, B, C].
        - Bảo đảm đọc lại cùng id không gây trùng lặp tác vụ nghiệp vụ (idempotency).
        - Toàn bộ trang sau đó được ghi nhận và next_cursor mới được lưu.
        """
        items = [
            {
                "id": 101,
                "cursor": 10,
                "filename": "ninja-acc-1-part.png",
                "status": "done",
                "result": {"text": "あいうえおか", "normalized_text": "あいうえおか", "warnings": []},
                "error": None,
            },
            {
                "id": 102,
                "cursor": 11,
                "filename": "ninja-acc-2-part.png",
                "status": "done",
                "result": {"text": "かきくけこさ", "normalized_text": "かきくけこさ", "warnings": []},
                "error": None,
            },
            {
                "id": 103,
                "cursor": 12,
                "filename": "ninja-acc-3-part.png",
                "status": "done",
                "result": {"text": "さしすせそた", "normalized_text": "さしすせそた", "warnings": []},
                "error": None,
            },
        ]
        self.mock_server_data["results"] = items

        config = OCRConfig(
            base_url="http://127.0.0.1:8000",
            inbox_dir=self.inbox_dir,
            state_file=self.state_file,
        )

        business_actions: Dict[int, int] = {101: 0, 102: 0, 103: 0}

        def record_action(item, norm_text):
            business_actions[item["id"]] += 1

        # LẦN CHẠY 1: Giả lập dừng giữa chừng sau khi xử lý mục đầu tiên (id: 101)
        client1 = HiraganaOCRClient(config)
        self.assertEqual(client1.state_store.next_cursor, 0)

        # Xử lý mục đầu tiên và đánh dấu đã thực hiện
        client1.state_store.mark_processed(101)
        record_action(items[0], "あいうえおか")

        # Giả lập sự cố: client1 bị tắt mà CHƯA LƯU cursor của trang (cursor vẫn là 0)
        client1.state_store.save()  # lưu processed_ids=[101], next_cursor=0

        # LẦN CHẠY 2: Khởi động lại ứng dụng
        client2 = HiraganaOCRClient(config)
        # Kiểm tra con trỏ vẫn là 0
        self.assertEqual(client2.state_store.next_cursor, 0)
        # ID 101 đã nằm trong danh sách đã xử lý
        self.assertTrue(client2.state_store.is_processed(101))

        # Đọc lại trang bắt đầu từ cursor 0
        processed_count = client2.poll_once(on_done=record_action)
        # client2 chỉ kích hoạt hành động cho các mục chưa xử lý (102, 103)
        self.assertEqual(processed_count, 2)

        # Đảm bảo mỗi id chỉ được thực thi nghiệp vụ DUY NHẤT 1 LẦN
        self.assertEqual(business_actions[101], 1)
        self.assertEqual(business_actions[102], 1)
        self.assertEqual(business_actions[103], 1)

        # Cuối cùng, sau khi toàn bộ trang hoàn tất, next_cursor được lưu tới 12
        self.assertEqual(client2.state_store.next_cursor, 12)

        # Kiểm tra lần đọc tiếp theo: không còn mục nào mới
        count_next = client2.poll_once()
        self.assertEqual(count_next, 0)

    # -------------------------------------------------------------------------
    # KIỂM THỬ GHI TỆP NGUYÊN TỬ (.part -> .png)
    # -------------------------------------------------------------------------
    def test_atomic_file_write(self):
        """Kiểm tra ghi tệp .part trước khi đổi tên sang .png vào thư mục inbox."""
        config = OCRConfig(base_url="http://127.0.0.1:8000", inbox_dir=self.inbox_dir)
        client = HiraganaOCRClient(config)

        filename = client.generate_filename(1, "test@gmail.com")
        image_data = b"\x89PNG\r\n\x1a\nfake_image_bytes"

        saved_path = client.submit_image(image_data, filename)

        self.assertTrue(saved_path.exists())
        self.assertEqual(saved_path.name, filename)
        self.assertEqual(saved_path.read_bytes(), image_data)
        # Đảm bảo tệp .part đã được dọn sạch qua lệnh rename
        part_path = self.inbox_dir / f"{filename}.part"
        self.assertFalse(part_path.exists())

    # -------------------------------------------------------------------------
    # KIỂM THỬ ĐỘ DÀI 6 KÝ TỰ (BUSINESS RULE VALIDATION)
    # -------------------------------------------------------------------------
    def test_hiragana_length_validation(self):
        """
        Nghiệp vụ Ninja yêu cầu đúng 6 ký tự Hiragana.
        Nếu kết quả có độ dài != 6 (ví dụ: 5 hoặc 7 ký tự),
        phải coi là không đạt chuẩn và ghi nhận thất bại / chuyển sang kiểm tra.
        """
        self.mock_server_data["results"] = [
            {
                "id": 201,
                "cursor": 20,
                "filename": "ninja-acc-1-invalid-len.png",
                "status": "done",
                "result": {
                    "text": "あいうえお",  # 5 ký tự
                    "normalized_text": "あいうえお",
                    "warnings": ["Thiếu 1 ký tự"],
                },
                "error": None,
            }
        ]

        config = OCRConfig(
            base_url="http://127.0.0.1:8000",
            inbox_dir=self.inbox_dir,
            state_file=self.state_file,
            expected_length=6,
        )
        client = HiraganaOCRClient(config)

        failed_reasons = []

        def on_failed(item, err):
            failed_reasons.append(err)

        processed = client.poll_once(on_failed=on_failed)
        self.assertEqual(processed, 1)
        self.assertEqual(len(failed_reasons), 1)
        self.assertIn("5 != 6", failed_reasons[0])
        self.assertEqual(len(client.state_store.failed_jobs), 1)

    # -------------------------------------------------------------------------
    # KIỂM THỬ OCR HOOK FALLBACK SANG MANUAL
    # -------------------------------------------------------------------------
    def test_ocr_hook_fallback_to_manual(self):
        """
        Khi OCR gặp lỗi hoặc không thể kết nối, OCRHiraganaHook
        tự động chuyển tiếp cuộc gọi sang ManualHiraganaHook mà không làm gãy luồng.
        """
        # Giả lập khi OCR throw lỗi kết nối (ví dụ server offline)
        config = OCRConfig(base_url="http://127.0.0.1:8000", inbox_dir=self.inbox_dir)
        client = HiraganaOCRClient(config)

        # Ghi đè _http_get để ném lỗi kết nối
        def mock_fail_get(path, params=None):
            raise OCRConnectionError("OCR Server offline")

        client._http_get = mock_fail_get

        manual_called = []

        async def mock_manual_cb(acc_idx, email):
            manual_called.append((acc_idx, email))
            return "てすとひらがな"

        manual_hook = ManualHiraganaHook(mock_manual_cb)
        ocr_hook = OCRHiraganaHook(
            ocr_client=client,
            fallback_hook=manual_hook,
            timeout=0.5,
        )

        result = run_coro(ocr_hook.solve(b"fake_image", 5, "ninja@test.com"))
        # Kết quả nhận được từ manual fallback
        self.assertEqual(result, "てすとひらがな")
        self.assertEqual(len(manual_called), 1)
        self.assertEqual(manual_called[0], (5, "ninja@test.com"))

    # -------------------------------------------------------------------------
    # KIỂM THỬ LỖI MẠNG / HTTP 500: GIỮ CON TRỎ VÀ THỬ LẠI
    # -------------------------------------------------------------------------
    def test_connection_error_preserves_cursor(self):
        """Khi có lỗi HTTP 500 hoặc rớt mạng, con trỏ phải được giữ nguyên."""
        config = OCRConfig(base_url="http://127.0.0.1:8000/error500", inbox_dir=self.inbox_dir, state_file=self.state_file)
        client = HiraganaOCRClient(config)
        client.state_store.next_cursor = 5

        with self.assertRaises(OCRConnectionError):
            client.poll_once()

        # Con trỏ vẫn giữ nguyên 5, không bị thay đổi hoặc mất dữ liệu
        self.assertEqual(client.state_store.next_cursor, 5)


if __name__ == "__main__":
    unittest.main()
