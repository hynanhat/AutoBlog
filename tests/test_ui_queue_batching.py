"""
Kiểm thử tính năng gom log (batching logs) trong _poll_ui_queue và thứ tự xử lý sự kiện UI.
"""

import queue
import unittest
from unittest.mock import MagicMock, patch

from app import NinjaBlogApp


class UIQueueBatchingTests(unittest.TestCase):
    def setUp(self):
        # Tạo mock cho NinjaBlogApp để kiểm thử logic _poll_ui_queue mà không cần mở Tkinter window thật
        self.app = MagicMock(spec=NinjaBlogApp)
        self.app.ui_queue = queue.Queue()
        self.app.txt_log = MagicMock()
        self.app.after = MagicMock()
        self.app.results_rows = {}
        self.app.hiragana_waiters = {}
        self.app.btn_start = MagicMock()
        self.app.btn_stop = MagicMock()

        # Gán method thật vào mock instance
        self.app._handle_ui_event = lambda kind, payload: NinjaBlogApp._handle_ui_event(self.app, kind, payload)
        self.app._poll_ui_queue = lambda: NinjaBlogApp._poll_ui_queue(self.app)
        self.app._finish_job = lambda payload: NinjaBlogApp._finish_job(self.app, payload)

    def test_thousand_logs_inserted_in_order(self):
        """Đưa 1.000 dòng log vào hàng đợi: hiển thị đủ từ 1 đến 1.000, đúng thứ tự."""
        for i in range(1, 1001):
            self.app.ui_queue.put(("log", f"Line {i}"))

        # Chạy _poll_ui_queue liên tục cho đến khi hàng đợi rỗng (mô phỏng chu trình after)
        iterations = 0
        while not self.app.ui_queue.empty() and iterations < 50:
            self.app._poll_ui_queue()
            iterations += 1

        self.assertTrue(self.app.ui_queue.empty())

        # Thu thập toàn bộ nội dung đã insert vào txt_log
        inserted_chunks = [call[0][1] for call in self.app.txt_log.insert.call_args_list]
        full_log_text = "".join(inserted_chunks)

        for i in range(1, 1001):
            self.assertIn(f"Line {i}\n", full_log_text)

        # Kiểm tra thứ tự xuất hiện
        pos = 0
        for i in range(1, 1001):
            next_pos = full_log_text.find(f"Line {i}\n", pos)
            self.assertNotEqual(next_pos, -1, f"Không tìm thấy Line {i} đúng thứ tự")
            pos = next_pos

        # Số lần gọi insert và see("end") giảm đáng kể so với 1.000 (gom theo đợt)
        self.assertLess(self.app.txt_log.insert.call_count, 100)
        self.assertEqual(self.app.txt_log.insert.call_count, self.app.txt_log.see.call_count)

    def test_event_ordering_flush_logs_before_other_events(self):
        """Đưa log A, log B, info, log C, finished: A và B xuất hiện trước info, C xuất hiện trước finished."""
        events_order = []

        def mock_handle_ui_event(kind, payload):
            # Ghi nhận trạng thái ô log tại thời điểm event này được xử lý
            current_log_calls = list(self.app.txt_log.insert.call_args_list)
            logged_texts = "".join(call[0][1] for call in current_log_calls)
            events_order.append((kind, payload, logged_texts))

        self.app._handle_ui_event = mock_handle_ui_event

        self.app.ui_queue.put(("log", "Log A"))
        self.app.ui_queue.put(("log", "Log B"))
        self.app.ui_queue.put(("info", ("Thành công", "Nội dung info")))
        self.app.ui_queue.put(("log", "Log C"))
        self.app.ui_queue.put(("finished", None))

        self.app._poll_ui_queue()

        self.assertEqual(len(events_order), 2)
        # Event 1: info -> Log A và Log B đã được flush trước đó
        first_event_kind, first_event_payload, text_at_info = events_order[0]
        self.assertEqual(first_event_kind, "info")
        self.assertIn("Log A", text_at_info)
        self.assertIn("Log B", text_at_info)
        self.assertNotIn("Log C", text_at_info)

        # Event 2: finished -> Log C đã được flush trước đó
        second_event_kind, second_event_payload, text_at_finished = events_order[1]
        self.assertEqual(second_event_kind, "finished")
        self.assertIn("Log C", text_at_finished)

    def test_scheduling_delay_adaptive(self):
        """Nếu hàng đợi còn sự kiện, delay là 10ms; nếu rỗng, delay là 150ms."""
        # 1. Khi rỗng
        self.app._poll_ui_queue()
        self.app.after.assert_called_with(150, self.app._poll_ui_queue)

        # 2. Khi còn hơn 100 items (vượt qua 1 lượt 100)
        self.app.after.reset_mock()
        for i in range(150):
            self.app.ui_queue.put(("log", f"Msg {i}"))

        self.app._poll_ui_queue()
        # Vẫn còn 50 items trong queue -> sau đó delay là 10ms để giải phóng nhanh
        self.app.after.assert_called_with(10, self.app._poll_ui_queue)


if __name__ == "__main__":
    unittest.main()
