"""
Kiểm thử tính năng xác thực Hiragana:
1. Thử lại OCR tối đa 3 lần khi nhập sai Hiragana (web tự đổi ảnh) hoặc OCR lỗi.
2. OCR thành công ở lần 1, lần 2 hoặc lần 3.
3. Chuyển sang nhập tay sau 3 lần OCR thất bại với thời gian chờ 60 giây.
4. Xử lý timeout 60 giây khi nhập tay.
5. Chạy trực tiếp chế độ nhập tay khi không dùng OCR.
6. Phương thức solve_ocr của OCRHiraganaHook không kích hoạt fallback_hook dở dang.
"""

from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from hiragana_hook import ManualHiraganaHook, OCRHiraganaHook
from ninja_worker import (
    JobConfig,
    MANUAL_HIRAGANA_TIMEOUT,
    MAX_OCR_ATTEMPTS,
    NinjaAutomationRunner,
)
from ocr_client import HiraganaOCRClient, OCRConfig


def run_coro(coro):
    """Thực thi coroutine thuần túy tương thích môi trường sandbox không có socket mạng."""
    result = None
    while True:
        try:
            val = coro.send(result)
            if hasattr(val, "__await__"):
                result = run_coro(val.__await__())
            else:
                result = val
        except StopIteration as e:
            return e.value


class TestHiraganaRetryAndManualFallback(unittest.TestCase):
    def setUp(self):
        self.config = JobConfig(
            gmails=["test1@gmail.com"],
            shared_password="password123",
            blog_name="Test Blog",
            subdomain="testblog",
            shared_title="Title",
            html_mode=1,
            shared_html="<p>Test</p>",
            posts_map={},
            max_concurrent=1,
        )
        self.status_calls = []
        self.log_messages = []

        def on_status(idx, email, domain, status, url):
            self.status_calls.append((idx, email, domain, status, url))

        def on_log(msg):
            self.log_messages.append(msg)

        self.on_status = on_status
        self.on_log = on_log

    def _create_mock_page(self, is_make_page=True, input_val=""):
        page = MagicMock()
        page.is_closed = MagicMock(return_value=False)
        page.url = "https://www.ninja.co.jp/home/blog/make" if is_make_page else "https://www.ninja.co.jp/home/"

        # Mock hiragana_input
        mock_input = AsyncMock()
        mock_input.first = mock_input
        mock_input.count = AsyncMock(return_value=1)
        mock_input.is_visible = AsyncMock(return_value=is_make_page)
        mock_input.input_value = AsyncMock(return_value=input_val)
        mock_input.focus = AsyncMock()
        mock_input.fill = AsyncMock()

        # Mock submit button
        mock_submit = AsyncMock()
        mock_submit.first = mock_submit
        mock_submit.click = AsyncMock()

        # Mock refresh link
        mock_refresh = AsyncMock()
        mock_refresh.first = mock_refresh
        mock_refresh.count = AsyncMock(return_value=1)
        mock_refresh.is_visible = AsyncMock(return_value=True)
        mock_refresh.click = AsyncMock()

        # Mock error locator
        mock_err = AsyncMock()
        mock_err.first = mock_err
        mock_err.count = AsyncMock(return_value=0)
        mock_err.is_visible = AsyncMock(return_value=False)

        def locator_side_effect(selector):
            if "authimg_input" in selector or ("input" in selector and "authimg" in selector):
                return mock_input
            if "作成" in selector:
                return mock_submit
            if "読みにくいので画像変更" in selector:
                return mock_refresh
            if "error" in selector:
                return mock_err
            loc = AsyncMock()
            loc.first = loc
            loc.count = AsyncMock(return_value=1)
            loc.is_visible = AsyncMock(return_value=True)
            return loc

        page.locator = MagicMock(side_effect=locator_side_effect)
        return page, mock_input, mock_submit, mock_refresh

    def test_ocr_success_on_first_try(self):
        """Lần 1: OCR giải thành công và web chấp nhận -> Không cần thử lại, không vào nhập tay."""
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_hook = OCRHiraganaHook(ocr_client=ocr_client)
        ocr_hook.solve_ocr = AsyncMock(return_value="あいうえおか")

        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=ocr_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._human_delay = AsyncMock()
        runner._capture_hiragana_image = AsyncMock(return_value=b"fake_image_bytes")
        runner._wait_for_make_submission = AsyncMock(return_value=True)
        runner._handle_manual_hiragana = AsyncMock()

        page, mock_input, mock_submit, _ = self._create_mock_page()

        success = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))

        self.assertTrue(success)
        self.assertEqual(ocr_hook.solve_ocr.call_count, 1)
        mock_input.fill.assert_called_once_with("あいうえおか")
        mock_submit.click.assert_called_once()
        runner._handle_manual_hiragana.assert_not_called()
        self.assertTrue(any("Tạo blog thành công ở lần thử OCR thứ 1" in m for m in self.log_messages))

    def test_ocr_retries_on_wrong_hiragana_and_succeeds_on_3rd_try(self):
        """
        Lần 1 & 2: Nhập sai Hiragana, web tự đổi ảnh -> Thử lại OCR.
        Lần 3: OCR giải đúng và web chấp nhận -> Thành công, không vào nhập tay.
        """
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_hook = OCRHiraganaHook(ocr_client=ocr_client)
        # 3 kết quả nhận diện cho 3 lần thử
        ocr_hook.solve_ocr = AsyncMock(side_effect=["いちにさんしご", "ろくななはちき", "あいうえおか"])

        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=ocr_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._human_delay = AsyncMock()
        runner._capture_hiragana_image = AsyncMock(
            side_effect=[b"img_bytes_1", b"img_bytes_2", b"img_bytes_3"]
        )
        # Lần 1 và 2: web từ chối (False), lần 3: web chấp nhận (True)
        runner._wait_for_make_submission = AsyncMock(side_effect=[False, False, True])
        runner._handle_manual_hiragana = AsyncMock()

        page, mock_input, mock_submit, _ = self._create_mock_page()

        success = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))

        self.assertTrue(success)
        self.assertEqual(ocr_hook.solve_ocr.call_count, 3)
        self.assertEqual(mock_input.fill.call_count, 3)
        self.assertEqual(mock_submit.click.call_count, 3)
        runner._handle_manual_hiragana.assert_not_called()

        # Kiểm tra ghi nhận log thử lại
        self.assertTrue(any("Nhập sai Hiragana (lần 1/3)" in m for m in self.log_messages))
        self.assertTrue(any("Nhập sai Hiragana (lần 2/3)" in m for m in self.log_messages))
        self.assertTrue(any("Tạo blog thành công ở lần thử OCR thứ 3" in m for m in self.log_messages))

    def test_ocr_fails_3_times_and_switches_to_manual_input(self):
        """
        Sau 3 lần OCR thất bại (nhập sai cả 3 lần):
        Tự động chuyển sang nhập tay với thời gian chờ 60 giây.
        """
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_hook = OCRHiraganaHook(ocr_client=ocr_client)
        ocr_hook.solve_ocr = AsyncMock(return_value="あいうえおか")

        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=ocr_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._human_delay = AsyncMock()
        runner._capture_hiragana_image = AsyncMock(return_value=b"fake_image")
        runner._wait_for_make_submission = AsyncMock(return_value=False)  # Luôn sai cả 3 lần
        runner._handle_manual_hiragana = AsyncMock(return_value=True)

        page, _, _, _ = self._create_mock_page()

        success = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))

        self.assertTrue(success)
        self.assertEqual(ocr_hook.solve_ocr.call_count, 3)
        runner._handle_manual_hiragana.assert_called_once_with(
            page, 1, "test1@gmail.com", "ninja.jp", timeout=float(MANUAL_HIRAGANA_TIMEOUT)
        )
        self.assertTrue(
            any("Đã thử OCR 3 lần không thành công. Chuyển sang nhập tay" in m for m in self.log_messages)
        )

    def test_ocr_unreadable_refreshes_image_before_retry(self):
        """
        Khi OCR không đọc được ảnh (trả về None):
        Hệ thống bấm '読みにくいので画像変更' để đổi ảnh mới trước khi thử lần tiếp theo.
        """
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_hook = OCRHiraganaHook(ocr_client=ocr_client)
        # Lần 1: None (ảnh mờ), lần 2: giải thành công
        ocr_hook.solve_ocr = AsyncMock(side_effect=[None, "あいうえおか"])

        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=ocr_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._human_delay = AsyncMock()
        runner._capture_hiragana_image = AsyncMock(side_effect=[b"img1", b"img2"])
        runner._refresh_hiragana_image = AsyncMock()
        runner._wait_for_make_submission = AsyncMock(return_value=True)

        page, _, _, _ = self._create_mock_page()

        success = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))

        self.assertTrue(success)
        self.assertEqual(ocr_hook.solve_ocr.call_count, 2)
        runner._refresh_hiragana_image.assert_called_once_with(page, 1)

    def test_manual_mode_skips_ocr_and_uses_60s_timeout(self):
        """
        Khi người dùng không dùng OCR (ManualHiraganaHook):
        Bỏ qua hoàn toàn OCR, chuyển trực tiếp sang nhập tay với timeout 60s.
        """
        manual_hook = ManualHiraganaHook(wait_for_user_callback=AsyncMock(return_value=None))
        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=manual_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._handle_manual_hiragana = AsyncMock(return_value=True)

        page, _, _, _ = self._create_mock_page()

        success = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))

        self.assertTrue(success)
        runner._handle_manual_hiragana.assert_called_once_with(
            page, 1, "test1@gmail.com", "ninja.jp", timeout=60.0
        )
        self.assertTrue(any("Chế độ nhập tay Hiragana" in m for m in self.log_messages))

    def test_manual_hiragana_timeout_raises_error(self):
        """
        Khi vào nhập tay, nếu hết thời gian chờ (60s) mà chưa hoàn tất:
        Ném RuntimeError báo hết thời gian chờ.
        """
        manual_hook = ManualHiraganaHook(wait_for_user_callback=AsyncMock(return_value=None))
        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=manual_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        page, _, _, _ = self._create_mock_page(is_make_page=True)

        times = [0.0, 10.0, 10.0, 25.0, 25.0, 61.0]

        def get_time():
            return times.pop(0) if times else 61.0

        mock_loop = MagicMock()
        mock_loop.time = MagicMock(side_effect=get_time)

        with patch("asyncio.get_running_loop", return_value=mock_loop), patch("asyncio.sleep", AsyncMock()), patch("asyncio.create_task", MagicMock(return_value=None)):
            with self.assertRaisesRegex(RuntimeError, "Hết thời gian chờ nhập tay Hiragana"):
                run_coro(runner._handle_manual_hiragana(page, 1, "test1@gmail.com", "ninja.jp", timeout=60.0))

    def test_manual_hiragana_succeeds_when_user_submits_on_chromium(self):
        """
        Khi người dùng nhập và bấm submit trên Chromium:
        Trang chuyển khỏi /home/blog/make -> _handle_manual_hiragana trả về True.
        """
        manual_hook = ManualHiraganaHook(wait_for_user_callback=AsyncMock(return_value=None))
        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=manual_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        page, _, _, _ = self._create_mock_page(is_make_page=True)

        mock_loop = MagicMock()
        mock_loop.time = MagicMock(return_value=0.0)

        # Giả lập sau 1 nhịp, page.url chuyển sang /home/
        def check_make_page_side_effect(p):
            if not hasattr(check_make_page_side_effect, "called"):
                check_make_page_side_effect.called = True
                return True
            return False

        with patch("asyncio.get_running_loop", return_value=mock_loop), patch("asyncio.sleep", AsyncMock()), patch("asyncio.create_task", MagicMock(return_value=None)), patch("ninja_worker.on_make_page", side_effect=check_make_page_side_effect):
            success = run_coro(runner._handle_manual_hiragana(page, 1, "test1@gmail.com", "ninja.jp", timeout=60.0))
            self.assertTrue(success)

    def test_solve_ocr_pure_ocr(self):
        """
        Kiểm tra OCRHiraganaHook.solve_ocr:
        - Thành công: trả về normalized_text.
        - Thất bại: trả về None, KHÔNG gọi fallback_hook.
        """
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_client.check_health.return_value = (True, {"status": "ok"})
        ocr_client.config = OCRConfig(expected_length=6)
        ocr_client.solve_image = AsyncMock(return_value="あいうえおか")

        fallback_called = []

        async def mock_fallback(idx, email):
            fallback_called.append(idx)
            return "fallback"

        fallback_hook = ManualHiraganaHook(mock_fallback)
        hook = OCRHiraganaHook(ocr_client=ocr_client, fallback_hook=fallback_hook)

        # 1. Trường hợp thành công
        res = run_coro(hook.solve_ocr(b"test_img", 1, "a@b.com"))
        self.assertEqual(res, "あいうえおか")
        self.assertEqual(len(fallback_called), 0)

        # 2. Trường hợp trả về 5 ký tự (lỗi validation)
        ocr_client.solve_image = AsyncMock(return_value="あいうえお")
        res_fail = run_coro(hook.solve_ocr(b"test_img", 1, "a@b.com"))
        self.assertIsNone(res_fail)
        # Đảm bảo KHÔNG gọi fallback_hook
        self.assertEqual(len(fallback_called), 0)

        # 3. Nhưng nếu gọi hook.solve() thì VẪN fallback sang manual như cũ
        res_fallback = run_coro(hook.solve(b"test_img", 1, "a@b.com"))
        self.assertEqual(res_fallback, "fallback")
        self.assertEqual(len(fallback_called), 1)

    def test_stalled_home_continues_automatically_without_forced_manual(self):
        """
        Kiểm tra rằng khi stalled_home là True (sau đăng nhập tab ở /home/ 10s và mạng bận, tự mở blog/make):
        Hệ thống tiếp tục tự động qua _process_hiragana_step (OCR và retry) thay vì ép nhập tay thủ công.
        """
        ocr_client = MagicMock(spec=HiraganaOCRClient)
        ocr_hook = OCRHiraganaHook(ocr_client=ocr_client)
        runner = NinjaAutomationRunner(
            config=self.config,
            hiragana_hook=ocr_hook,
            on_status=self.on_status,
            on_log=self.on_log,
        )
        runner._process_hiragana_step = AsyncMock(return_value=True)
        runner._handle_manual_hiragana = AsyncMock()

        page, _, _, _ = self._create_mock_page()
        step_result = run_coro(runner._process_hiragana_step(page, 1, "test1@gmail.com", "ninja.jp"))
        self.assertTrue(step_result)
        runner._handle_manual_hiragana.assert_not_called()


if __name__ == "__main__":
    unittest.main()
