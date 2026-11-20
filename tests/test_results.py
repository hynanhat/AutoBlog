import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import NinjaBlogApp
from ninja_worker import PUBLIC_POST_FOUND, PUBLICATION_UNCONFIRMED


class JobResultTests(unittest.TestCase):
    def test_unconfirmed_publication_survives_summary_and_csv_data(self):
        rows = {1: {"status": PUBLICATION_UNCONFIRMED}, 2: {"status": PUBLIC_POST_FOUND}}
        app = SimpleNamespace(results_rows=rows, btn_start=Mock(), btn_stop=Mock())
        with patch("app.messagebox") as dialog:
            NinjaBlogApp._finish_job(app, None)
        self.assertEqual(rows[1]["status"], PUBLICATION_UNCONFIRMED)
        dialog.showwarning.assert_called_once()
        self.assertIn("Thành công: 1\nLỗi/Hủy: 0", dialog.showwarning.call_args.args[1])

    def test_finish_updates_unfinished_rows_and_preserves_final_results(self):
        statuses = ["DONE", "LỖI: timeout", "ĐÃ HỦY", "Đang chờ", "Đang đăng bài HTML..."]
        for failure, expected in [(None, "Chưa hoàn tất"), ("Mất kết nối", "Phiên chạy bị gián đoạn")]:
            with self.subTest(failure=failure), patch("app.messagebox") as dialog:
                rows = {
                    idx: {"status": status, "widgets": (None, Mock(), None)}
                    for idx, status in enumerate(statuses, start=1)
                }
                app = SimpleNamespace(results_rows=rows, btn_start=Mock(), btn_stop=Mock())
                NinjaBlogApp._finish_job(app, failure)
                self.assertEqual([row["status"] for row in rows.values()], statuses[:3] + [expected] * 2)
                for idx in (4, 5):
                    rows[idx]["widgets"][1].configure.assert_called_once_with(text=expected)
                if failure is None:
                    dialog.showwarning.assert_called_once_with(
                        "Chưa hoàn tất toàn bộ", "Thành công: 1\nLỗi/Hủy: 2\nChưa hoàn tất: 2"
                    )
                else:
                    dialog.showerror.assert_called_once_with("Phiên chạy bị gián đoạn", failure)
