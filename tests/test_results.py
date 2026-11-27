import csv
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app import NinjaBlogApp
from ninja_worker import PUBLIC_POST_FOUND, PUBLICATION_UNCONFIRMED


class JobResultTests(unittest.TestCase):
    def test_export_only_created_blog_urls_in_row_order(self):
        rows = {
            6: {"status": "LỖI: timeout", "url": "https://failed-publication.example.test/"},
            7: {"status": "ĐÃ HỦY", "url": "https://cancelled-publication.example.test/"},
            8: {"status": "LỖI: login", "url": ""},
            5: {"status": PUBLICATION_UNCONFIRMED, "url": "https://review.example.test/"},
            3: {"status": "DONE", "url": ""},
            4: {"status": PUBLIC_POST_FOUND, "url": "https://public.example.test/"},
            1: {"status": "DONE", "url": "https://done.example.test/"},
            2: {"status": "Đang chờ", "url": ""},
        }
        app = SimpleNamespace(results_rows=rows)
        with TemporaryDirectory() as tmp, patch("app.filedialog") as picker, patch("app.messagebox") as dialog:
            filename = Path(tmp) / "blogs.csv"
            picker.asksaveasfilename.return_value = str(filename)

            NinjaBlogApp._export_csv(app)

            with filename.open(newline="", encoding="utf-8-sig") as file:
                self.assertEqual(list(csv.reader(file)), [
                    ["URL"],
                    ["https://done.example.test/"],
                    ["https://public.example.test/"],
                    ["https://review.example.test/"],
                    ["https://failed-publication.example.test/"],
                    ["https://cancelled-publication.example.test/"],
                ])
            dialog.showinfo.assert_called_once()

    def test_export_does_not_prompt_when_no_created_blog_has_a_url(self):
        rows = {1: {"status": "Đang chờ", "url": ""}, 2: {"status": "DONE", "url": ""}}
        app = SimpleNamespace(results_rows=rows)
        with patch("app.filedialog") as picker, patch("app.messagebox") as dialog:
            NinjaBlogApp._export_csv(app)
        picker.asksaveasfilename.assert_not_called()
        dialog.showinfo.assert_called_once()

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

    def test_copy_entire_row(self):
        copied = []
        rows = {
            1: {
                "email": "test@gmail.com",
                "domain": "ninja.jp",
                "status": "Hoàn tất",
                "url": "https://myblog.ninja.jp/",
                "all_widgets": (),
            }
        }
        app = SimpleNamespace(
            results_rows=rows,
            _flash_row=Mock(),
            _copy_to_clipboard=lambda text, msg="": copied.append((text, msg)),
        )
        NinjaBlogApp._copy_entire_row(app, 1)
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0][0], "1\ttest@gmail.com\tninja.jp\tHoàn tất\thttps://myblog.ninja.jp/")
        self.assertIn("Dòng #1", copied[0][1])

    def test_copy_url_cell(self):
        copied = []
        rows = {
            1: {
                "email": "test@gmail.com",
                "domain": "ninja.jp",
                "status": "Hoàn tất",
                "url": "https://myblog.ninja.jp/",
                "all_widgets": (),
            }
        }
        app = SimpleNamespace(
            results_rows=rows,
            _flash_row=Mock(),
            _copy_to_clipboard=lambda text, msg="": copied.append((text, msg)),
            _copy_entire_row=Mock(),
        )
        NinjaBlogApp._copy_cell(app, 1, "url")
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0][0], "https://myblog.ninja.jp/")
        self.assertIn("Link Blog", copied[0][1])
        app._copy_entire_row.assert_not_called()

    def test_empty_url_does_not_copy_a_row(self):
        rows = {
            1: {
                "email": "test@gmail.com",
                "domain": "-",
                "status": "Đang chờ",
                "url": "",
                "all_widgets": (),
            }
        }
        app = SimpleNamespace(
            results_rows=rows,
            _flash_row=Mock(),
            _copy_entire_row=Mock(),
            _copy_to_clipboard=Mock(),
        )
        NinjaBlogApp._copy_cell(app, 1, "url")
        app._copy_entire_row.assert_not_called()
        app._copy_to_clipboard.assert_not_called()

    def test_copy_email_cell(self):
        copied = []
        rows = {
            1: {
                "email": "test@gmail.com",
                "domain": "-",
                "status": "Đang chờ",
                "url": "",
                "all_widgets": (),
            }
        }
        app = SimpleNamespace(
            results_rows=rows,
            _flash_row=Mock(),
            _copy_to_clipboard=lambda text, msg="": copied.append((text, msg)),
            _copy_entire_row=Mock(),
        )
        NinjaBlogApp._copy_cell(app, 1, "email")
        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0][0], "test@gmail.com")
        self.assertIn("Gmail", copied[0][1])

    def test_row_menu_waits_for_copy_choice(self):
        app = SimpleNamespace(
            results_rows={1: {"email": "test@gmail.com", "url": "https://blog.example.test/"}},
            _copy_cell=Mock(),
            _copy_entire_row=Mock(),
        )
        with patch("app.show_row_menu") as menu:
            NinjaBlogApp._show_row_context_menu(app, Mock(), 1)
        app._copy_cell.assert_not_called()
        app._copy_entire_row.assert_not_called()

        options = dict(menu.call_args.args[2])
        options["Sao chép link blog"]()
        app._copy_cell.assert_called_once_with(1, "url")
        app._copy_entire_row.assert_not_called()
