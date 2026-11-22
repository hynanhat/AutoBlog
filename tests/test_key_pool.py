"""
Kiểm thử bộ quản lý API Key Pool và cơ chế xoay vòng Model Fallback.
Bảo đảm kiểm soát 2 tầng hạn mức:
1. RPM (12/15) - Chủ động chuyển key trong 60 giây.
2. RPD (450/500) - Chủ động fallback sang Model tiếp theo hoặc loại trừ key trong ngày.
3. Lưu trữ bền vững số liệu ngày xuống tệp .key_usage.json.
"""

from collections import deque
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from ocr.key_pool import (
    APIKeyPool,
    TrackedKeyModel,
    load_keys_from_sources,
    mask_key,
    usage_key_id,
)


class MockGenAIClient:
    def __init__(self, key: str):
        self.key = key
        self.models = MagicMock()
        self.closed = False

    def close(self):
        self.closed = True


class KeyPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.usage_file = Path(self.temp_dir.name) / ".key_usage.json"
        self.keys_file = Path(self.temp_dir.name) / "api_keys.txt"

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mask_key(self):
        self.assertEqual(mask_key("AIzaSyAbcd1234efgh"), "AIza...efgh")
        self.assertEqual(mask_key("short"), "***")

    def test_load_keys_from_sources(self):
        self.keys_file.write_text("key_file_1\n# comment line\nkey_file_2\nkey_file_1\n", encoding="utf-8")
        with patch.dict("os.environ", {"GEMINI_API_KEYS": "key_env_1,key_env_2", "GEMINI_API_KEY": "key_file_1"}):
            keys = load_keys_from_sources(api_keys_file=self.keys_file)
            self.assertIn("key_file_1", keys)
            self.assertIn("key_file_2", keys)
            self.assertIn("key_env_1", keys)
            self.assertIn("key_env_2", keys)
            # Không trùng lặp
            self.assertEqual(len(keys), len(set(keys)))

    def test_tracked_key_rpm_sliding_window(self):
        slot = TrackedKeyModel(key="k1", model="gemini-3.1-flash-lite", max_rpm=3, max_rpd=10)
        now = 1000.0
        day = 1

        # Ban đầu an toàn
        self.assertTrue(slot.is_safe_to_use(now, day))
        self.assertEqual(slot.current_rpm(now), 0)

        # Gửi 2 request
        slot.record_request(now, day)
        slot.record_request(now + 1.0, day)
        self.assertEqual(slot.current_rpm(now + 2.0), 2)
        self.assertTrue(slot.is_safe_to_use(now + 2.0, day))

        # Gửi request thứ 3 -> đạt max_rpm (3)
        slot.record_request(now + 3.0, day)
        self.assertEqual(slot.current_rpm(now + 4.0), 3)
        # Không còn an toàn vì đã chạm 3 RPM
        self.assertFalse(slot.is_safe_to_use(now + 4.0, day))

        # Sau 60.5 giây kể từ request đầu tiên, chỉ request đầu tiên trôi khỏi cửa sổ 60s
        self.assertTrue(slot.is_safe_to_use(now + 60.5, day))
        self.assertEqual(slot.current_rpm(now + 60.5), 2)

    def test_tracked_key_rpd_exhaustion(self):
        slot = TrackedKeyModel(key="k1", model="gemini-3.1-flash-lite", max_rpm=10, max_rpd=3)
        now = 1000.0
        day = 1

        slot.record_request(now, day)
        slot.record_request(now + 1.0, day)
        self.assertTrue(slot.is_safe_to_use(now + 2.0, day))

        slot.record_request(now + 3.0, day)
        self.assertEqual(slot.daily_requests, 3)
        # Đã chạm 3 RPD -> Hết hạn mức ngày
        self.assertFalse(slot.is_safe_to_use(now + 4.0, day))

        # Sang ngày mới (day = 2) -> Tự động hồi phục
        self.assertTrue(slot.is_safe_to_use(now + 100.0, 2))
        self.assertEqual(slot.daily_requests, 0)

    def test_pool_proactive_rpm_switch_between_keys(self):
        """Khi Key 1 đạt ngưỡng 2 RPM, pool tự động chuyển sang Key 2 để không chạm trần."""
        client_map = {}
        def mock_factory(k):
            c = MockGenAIClient(k)
            client_map[k] = c
            return c

        pool = APIKeyPool(
            keys=["key_A", "key_B"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=2,
            max_rpd=100,
            client_factory=mock_factory,
        )

        # Request 1 -> chọn Key A
        k1, m1, _ = pool.acquire_key()
        self.assertEqual(k1, "key_A")

        # Request 2 -> Key B rảnh hơn (0 RPM < 1 RPM) -> chọn Key B
        k2, m2, _ = pool.acquire_key()
        self.assertEqual(k2, "key_B")

        # Request 3 -> cả 2 đều 1 RPM -> chọn Key A
        k3, m3, _ = pool.acquire_key()
        self.assertEqual(k3, "key_A")

        # Bây giờ Key A đã có 2 RPM (đầy) -> Request 4 PHẢI chọn Key B
        k4, m4, _ = pool.acquire_key()
        self.assertEqual(k4, "key_B")

        pool.close()

    def test_pool_proactive_rpd_fallback_to_second_model(self):
        """
        Khi gemini-3.1-flash-lite hết hạn mức ngày (450 RPD),
        hệ thống tự động chuyển sang gemini-3.5-flash-lite!
        """
        pool = APIKeyPool(
            keys=["key_single"],
            models=["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=10,
            max_rpd=2,
            client_factory=lambda k: MockGenAIClient(k),
        )

        # 2 request đầu dùng model 3.1
        k1, m1, _ = pool.acquire_key()
        self.assertEqual(m1, "gemini-3.1-flash-lite")
        k2, m2, _ = pool.acquire_key()
        self.assertEqual(m2, "gemini-3.1-flash-lite")

        # Bây giờ model 3.1 đã đạt 2 RPD (hết hạn mức ngày).
        # Request 3 PHẢI tự động fallback sang gemini-3.5-flash-lite!
        k3, m3, _ = pool.acquire_key()
        self.assertEqual(k3, "key_single")
        self.assertEqual(m3, "gemini-3.5-flash-lite")

        pool.close()

    def test_pool_persistence_across_restarts(self):
        """Kiểm tra lưu bền vững xuống tệp JSON để không mất số đếm khi server khởi động lại."""
        pool1 = APIKeyPool(
            keys=["key_P"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=10,
            max_rpd=50,
            client_factory=lambda k: MockGenAIClient(k),
        )
        pool1.acquire_key()
        pool1.acquire_key()
        pool1.acquire_key()
        self.assertEqual(pool1._tracked[("key_P", "gemini-3.1-flash-lite")].daily_requests, 3)
        pool1.close()

        self.assertTrue(self.usage_file.is_file())
        file_content = self.usage_file.read_text(encoding="utf-8")
        # Bảo mật: không chứa chuỗi khóa thô
        self.assertNotIn("key_P", file_content)
        # Sử dụng mã băm SHA-256
        self.assertIn(usage_key_id("key_P"), file_content)
        data = json.loads(file_content)
        self.assertEqual(data.get("schema_version"), 2)

        # Giả lập server khởi động lại (khởi tạo pool mới với cùng usage_file)
        pool2 = APIKeyPool(
            keys=["key_P"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=10,
            max_rpd=50,
            client_factory=lambda k: MockGenAIClient(k),
        )
        self.assertEqual(pool2._tracked[("key_P", "gemini-3.1-flash-lite")].daily_requests, 3)
        pool2.close()

    def test_schema_migration_v1_to_v2(self):
        """Kiểm tra tự động chuyển đổi file v1 (chứa raw key) sang schema v2 (SHA-256) an toàn."""
        from datetime import datetime, timezone
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        v1_payload = {
            "date": today_str,
            "usage": {
                "raw_secret_key_999": {
                    "gemini-3.1-flash-lite": {
                        "daily_requests": 7,
                        "total_requests": 14,
                        "max_rpd": 450,
                        "status": "active"
                    }
                }
            }
        }
        self.usage_file.write_text(json.dumps(v1_payload), encoding="utf-8")

        # Nạp pool với key này
        pool = APIKeyPool(
            keys=["raw_secret_key_999"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            client_factory=lambda k: MockGenAIClient(k),
        )
        # Kiểm tra khôi phục thành công 7 requests
        self.assertEqual(pool._tracked[("raw_secret_key_999", "gemini-3.1-flash-lite")].daily_requests, 7)

        # File đã được chuyển đổi sang schema_version 2 và không còn raw key
        migrated_content = self.usage_file.read_text(encoding="utf-8")
        self.assertNotIn("raw_secret_key_999", migrated_content)
        self.assertIn(usage_key_id("raw_secret_key_999"), migrated_content)
        migrated_data = json.loads(migrated_content)
        self.assertEqual(migrated_data.get("schema_version"), 2)
        pool.close()

    def test_concurrent_workers_no_deadlock(self):
        """Kiểm tra 2 worker đồng thời acquire_key và ghi thống kê không bao giờ bị deadlock."""
        import concurrent.futures

        pool = APIKeyPool(
            keys=["worker_key_1", "worker_key_2"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=50,
            max_rpd=100,
            client_factory=lambda k: MockGenAIClient(k),
        )

        def worker_task(idx):
            key, model, _ = pool.acquire_key()
            return key, model

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(worker_task, i) for i in range(10)]
            # Chờ hoàn thành với timeout 5s để chứng minh không bị deadlock
            results = [f.result(timeout=5.0) for f in futures]

        self.assertEqual(len(results), 10)
        total_daily = sum(slot.daily_requests for slot in pool._tracked.values())
        self.assertEqual(total_daily, 10)
        pool.close()

    def test_pool_record_rate_limit_penalty(self):
        """Khi bị Google phạt 429 đột xuất, key bị tạm ngưng và chuyển ngay sang key khác."""
        pool = APIKeyPool(
            keys=["key_1", "key_2"],
            models=["gemini-3.1-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=10,
            max_rpd=50,
            client_factory=lambda k: MockGenAIClient(k),
        )
        # Phạt key_1 20 giây
        pool.record_rate_limit("key_1", "gemini-3.1-flash-lite", 20.0)

        # Lấy key tiếp theo -> phải là key_2
        k, m, _ = pool.acquire_key()
        self.assertEqual(k, "key_2")
        pool.close()

    def test_pool_status_summary(self):
        pool = APIKeyPool(
            keys=["AIzaSySampleKey123456789"],
            models=["gemini-3.1-flash-lite", "gemini-3.5-flash-lite"],
            usage_file=self.usage_file,
            max_rpm=12,
            max_rpd=450,
            client_factory=lambda k: MockGenAIClient(k),
        )
        summary = pool.get_status_summary()
        self.assertEqual(summary["total_keys"], 1)
        self.assertEqual(len(summary["models"]), 2)
        self.assertEqual(len(summary["details"]), 2)
        self.assertEqual(summary["details"][0]["key_masked"], "AIza...6789")
        self.assertEqual(summary["details"][0]["status"], "active")
        pool.close()


if __name__ == "__main__":
    unittest.main()
