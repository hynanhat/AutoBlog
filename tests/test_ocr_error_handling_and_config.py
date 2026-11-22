"""
Kiểm thử xử lý lỗi HTTP (429, 403), bảo vệ ReadingCache và ưu tiên cấu hình --model / GEMINI_MODELS.
"""

from contextlib import ExitStack
from functools import partial
import math
import numpy as np
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

from google.genai import errors
from ocr.config import configured_models
from ocr.key_pool import APIKeyPool
from ocr.ocr_service import OCRService
from ocr.server import main


class MockResponse:
    def __init__(self, text: str):
        self.text = text
        self.usage_metadata = MagicMock()
        self.usage_metadata.total_token_count = 10


def make_dummy_image():
    # Ảnh dummy 64x64 xám
    return np.zeros((64, 64, 3), dtype=np.uint8)


class OCRErrorHandlingAndConfigTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)

        folder = Path(stack.enter_context(TemporaryDirectory()))
        self.make_pool = partial(
            APIKeyPool,
            usage_file=folder / ".key_usage.json",
        )

        stack.enter_context(patch("ocr.server.APIKeyPool", self.make_pool))
        stack.enter_context(patch("ocr.server.load_dotenv"))
        stack.enter_context(
            patch("ocr.server.load_keys_from_sources", return_value=["test_k1"])
        )

    def test_interactive_429_raises_and_cache_empty(self):
        """Client trả 429 ở chế độ tương tác: Nhận RuntimeError có HTTP 429, cache vẫn rỗng."""
        client = MagicMock()
        mock_exc = errors.APIError(429, "Resource has been exhausted (e.g. check quota)")
        mock_exc.response = MagicMock()
        mock_exc.response.headers = {"Retry-After": "2"}
        mock_exc.details = None
        client.models.generate_content.side_effect = mock_exc

        pool = self.make_pool(
            keys=["test_k1"],
            models=["gemini-3.1-flash-lite"],
            client_factory=lambda key: client,
        )
        service = OCRService(key_pool=pool, delay=0.0)
        image = make_dummy_image()

        with self.assertRaises(RuntimeError) as ctx:
            service.recognize(image, interactive=True)

        self.assertIn("HTTP 429", str(ctx.exception))
        # Cache không lưu None, vẫn hoàn toàn rỗng
        self.assertEqual(len(service.cache.entries), 0)

    def test_interactive_403_permission_denied(self):
        """Client trả 403 PERMISSION_DENIED: Nhận lỗi có HTTP 403. Khóa vẫn được coi là hợp lệ."""
        client = MagicMock()
        mock_exc = errors.APIError(403, "PERMISSION_DENIED: Caller does not have required permission to use model")
        client.models.generate_content.side_effect = mock_exc

        pool = self.make_pool(
            keys=["test_k1"],
            models=["gemini-3.1-flash-lite"],
            client_factory=lambda key: client,
        )
        service = OCRService(key_pool=pool, delay=0.0)
        image = make_dummy_image()

        with self.assertRaises(RuntimeError) as ctx:
            service.recognize(image, interactive=True)

        self.assertIn("HTTP 403", str(ctx.exception))
        # Khóa KHÔNG bị đánh dấu hỏng
        self.assertFalse(pool._tracked[("test_k1", "gemini-3.1-flash-lite")].is_invalid)
        # Cache vẫn rỗng
        self.assertEqual(len(service.cache.entries), 0)

    def test_retry_same_image_after_delay_uses_cache_on_third_call(self):
        """Sau thời gian chờ, client trả kết quả hợp lệ cho cùng ảnh: lần 2 thành công, lần 3 dùng cache."""
        client = MagicMock()
        mock_exc = errors.APIError(429, "Rate limit exceeded")
        mock_exc.response = MagicMock()
        mock_exc.response.headers = {"Retry-After": "1"}
        mock_exc.details = None

        success_response = MockResponse('{"raw_hiragana": "あいうえおか", "confidence_note": ""}')

        # Lần 1: lỗi 429
        # Lần 2: thành công
        client.models.generate_content.side_effect = [mock_exc, success_response]

        pool = self.make_pool(
            keys=["test_k1"],
            models=["gemini-3.1-flash-lite"],
            client_factory=lambda key: client,
        )
        service = OCRService(key_pool=pool, delay=0.0)
        image = make_dummy_image()

        # Lần 1: Báo lỗi 429
        with self.assertRaises(RuntimeError) as ctx:
            service.recognize(image, interactive=True)
        self.assertIn("HTTP 429", str(ctx.exception))
        self.assertEqual(len(service.cache.entries), 0)

        # Lần 2: Thử lại đúng cùng ảnh đó -> Thành công!
        result2 = service.recognize(image, interactive=True)
        self.assertEqual(result2["text"], "あいうえおか")
        self.assertEqual(len(service.cache.entries), 1)
        self.assertEqual(service.usage.cache_hits, 0)

        # Lần 3: Gọi lại đúng ảnh đó -> Đọc từ cache, không gọi client
        result3 = service.recognize(image, interactive=True)
        self.assertEqual(result3["text"], "あいうえおか")
        self.assertEqual(service.usage.cache_hits, 1)

    def test_server_gemini_models_env_only(self):
        """Chỉ đặt GEMINI_MODELS=model-a,model-b: Server nhận đúng ['model-a', 'model-b']."""
        with patch.dict(os.environ, {"GEMINI_MODELS": "model-a,model-b", "GEMINI_API_KEY": "dummy_key"}):
            with patch("ocr.server.uvicorn.run") as mock_uvicorn, patch("ocr.server.create_app") as mock_create_app:
                main(["--port", "8000"])
                self.assertTrue(mock_create_app.called)
                service_arg = mock_create_app.call_args[0][1]
                self.assertEqual(service_arg.key_pool.models, ["model-a", "model-b"])

    def test_server_gemini_models_with_cli_model_arg(self):
        """Thêm --model model-b: Server nhận ['model-b', 'model-a']."""
        with patch.dict(os.environ, {"GEMINI_MODELS": "model-a,model-b", "GEMINI_API_KEY": "dummy_key"}):
            with patch("ocr.server.uvicorn.run") as mock_uvicorn, patch("ocr.server.create_app") as mock_create_app:
                main(["--model", "model-b", "--port", "8000"])
                self.assertTrue(mock_create_app.called)
                service_arg = mock_create_app.call_args[0][1]
                self.assertEqual(service_arg.key_pool.models, ["model-b", "model-a"])

    def test_server_gemini_models_empty_comma_spaces_exits_with_error(self):
        """Đặt GEMINI_MODELS=' , ': CLI báo 'GEMINI_MODELS phải chứa ít nhất một tên model' và thoát."""
        with patch.dict(os.environ, {"GEMINI_MODELS": " , ", "GEMINI_API_KEY": "dummy_key"}):
            with patch("ocr.server.uvicorn.run") as mock_uvicorn:
                with self.assertRaises(SystemExit):
                    with patch("sys.stderr"):
                        main([])
                mock_uvicorn.assert_not_called()


    def test_401_single_key_raises_runtime_error_and_marks_invalid(self):
        """Khóa duy nhất bị 401: Báo lỗi hết hạn/không hợp lệ và đánh dấu slot invalid."""
        client = MagicMock()
        mock_exc = errors.APIError(401, "UNAUTHENTICATED: API key expired or invalid")
        client.models.generate_content.side_effect = mock_exc

        pool = self.make_pool(
            keys=["bad_k1"],
            models=["gemini-3.1-flash-lite"],
            client_factory=lambda key: client,
        )
        service = OCRService(key_pool=pool, delay=0.0)
        image = make_dummy_image()

        with self.assertRaises(RuntimeError) as ctx:
            service.recognize(image, interactive=False)

        self.assertIn("HTTP 401", str(ctx.exception))
        self.assertTrue(pool._tracked[("bad_k1", "gemini-3.1-flash-lite")].is_invalid)

    def test_401_multi_key_rotates_to_valid_key_successfully(self):
        """Hồ bơi 2 key, key 1 bị 401: Tự động đánh dấu key 1 hỏng và xoay sang key 2 thành công."""
        client_bad = MagicMock()
        mock_exc = errors.APIError(401, "UNAUTHENTICATED: Invalid credentials")
        client_bad.models.generate_content.side_effect = mock_exc

        client_good = MagicMock()
        success_response = MockResponse('{"raw_hiragana": "あいうえおか", "confidence_note": ""}')
        client_good.models.generate_content.return_value = success_response

        def factory(key):
            if key == "bad_k1":
                return client_bad
            return client_good

        pool = self.make_pool(
            keys=["bad_k1", "good_k2"],
            models=["gemini-3.1-flash-lite"],
            client_factory=factory,
        )
        service = OCRService(key_pool=pool, delay=0.0)
        image = make_dummy_image()

        res = service.recognize(image, interactive=False)
        self.assertEqual(res["text"], "あいうえおか")
        self.assertTrue(pool._tracked[("bad_k1", "gemini-3.1-flash-lite")].is_invalid)
        self.assertFalse(pool._tracked[("good_k2", "gemini-3.1-flash-lite")].is_invalid)


if __name__ == "__main__":
    unittest.main()
