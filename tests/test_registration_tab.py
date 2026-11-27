import csv
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import MagicMock, Mock, patch

from app import NinjaBlogApp

from gmail_fetcher import ImapConfig, STATUS_CONFIRMED
from ocr_client import resolve_default_ocr_url
from registration_tab import RegistrationTab
from registration_worker import (
    RegistrationWorkerController,
    STATUS_CANCELLED,
    STATUS_FORM_HANDOFF,
    STATUS_FORM_READY,
    STATUS_PENDING,
    STATUS_REGISTERED,
    STATUS_SEND_UNKNOWN,
    STATUS_TIMEOUT,
)
from tests.test_gmail_fetcher import FakeImapConnection, FakeImapServerState


class RegistrationTabTests(unittest.TestCase):
    def setUp(self):
        self.tab = MagicMock(spec=RegistrationTab)
        self.tab.ui_queue = queue.Queue()
        self.tab.txt_log = MagicMock()
        self.tab.after = MagicMock(return_value="after-1")
        self.tab._after_id = None
        self.tab._is_closing = False
        self.tab._is_running = False
        self.tab.results_rows = {}
        self.tab.table_frame = MagicMock()
        self.tab.btn_start_reg = MagicMock()
        self.tab.btn_stop_reg = MagicMock()
        self.tab.btn_retry_mail = MagicMock()
        self.tab.btn_login = MagicMock()
        self.tab.entry_inbox_user = MagicMock()
        self.tab._inbox_email = Mock()
        self.tab._inbox_email.get.return_value = "ab@gmail.com"
        self.tab._connected_mailbox = ImapConfig("ab@gmail.com", oauth_token=Mock())
        self.tab._batch_email = "ab@gmail.com"
        self.tab._login_pending = None
        self.tab.controller = MagicMock(spec=RegistrationWorkerController)
        self.tab._imap_factory = None

        self.tab._handle_ui_event = lambda k, p: RegistrationTab._handle_ui_event(self.tab, k, p)
        self.tab._poll_ui_queue = lambda: RegistrationTab._poll_ui_queue(self.tab)
        self.tab._set_controls_busy = lambda b: RegistrationTab._set_controls_busy(self.tab, b)
        self.tab._finish_registration = lambda f: RegistrationTab._finish_registration(self.tab, f)
        self.tab._finish_retry = lambda f, c: RegistrationTab._finish_retry(self.tab, f, c)
        self.tab._sync_controls = lambda: RegistrationTab._sync_controls(self.tab)

    @staticmethod
    def _close_app(app):
        app._on_closing()
        for callback in app.tk.splitlist(app.tk.call("after", "info")):
            app.tk.call("after", "cancel", callback)

    def test_ui_queue_batching_and_status_updates_only_display_fields(self):
        lbl_status = MagicMock()
        lbl_time = MagicMock()
        self.tab.results_rows = {
            1: {
                "email": "a.b@gmail.com",
                "status": STATUS_PENDING,
                "updated_at": "-",
                "widgets": (lbl_status, lbl_time),
                "all_widgets": (),
            }
        }
        for i in range(1, 51):
            self.tab.ui_queue.put(("log", f"Log {i}"))
        self.tab.ui_queue.put(("status", (1, "a.b@gmail.com", STATUS_CONFIRMED, "10:15:00")))

        self.tab._poll_ui_queue()
        self.assertTrue(self.tab.ui_queue.empty())
        self.assertEqual(self.tab.results_rows[1]["status"], STATUS_CONFIRMED)
        self.assertEqual(self.tab.results_rows[1]["updated_at"], "10:15:00")
        lbl_status.configure.assert_called_once_with(text=STATUS_CONFIRMED)
        lbl_time.configure.assert_called_once_with(text="10:15:00")
        self.assertEqual(sorted(self.tab.results_rows[1].keys()), ["all_widgets", "email", "status", "updated_at", "widgets"])

    def test_invalid_inputs_preserve_form_and_never_start_controller(self):
        self.tab.txt_reg_gmails = MagicMock()
        self.tab.txt_reg_gmails.get.return_value = "a.b@gmail.com\nwrong@other.com\n"
        self.tab.entry_wait_timeout = MagicMock()
        self.tab.entry_wait_timeout.get.return_value = "120"

        with patch("registration_tab.messagebox") as mb:
            RegistrationTab._start_registration(self.tab)

        mb.showerror.assert_called_once()
        self.assertIn("Dòng 2", mb.showerror.call_args.args[1])
        self.tab.controller.start_batch.assert_not_called()
        self.tab.txt_reg_gmails.delete.assert_not_called()

    def test_login_and_one_time_setup_through_the_ui(self):
        state = FakeImapServerState()
        proceed = threading.Event()

        def google_consent(**kwargs):
            self.assertEqual(kwargs["login_hint"], state.valid_user)
            self.assertTrue(proceed.wait(4))
            return Mock(token="mock-access-token", valid=True)

        with TemporaryDirectory() as folder, patch("gmail_fetcher.__file__", str(Path(folder) / "app.py")), \
             patch("gmail_fetcher._user_oauth_client_file", return_value=Path(folder) / "saved.json"), \
             patch("gmail_fetcher.InstalledAppFlow.run_local_server", side_effect=google_consent) as google, \
             patch("registration_tab.load_gmail_credentials", return_value=("", "")):
            source = Path(folder) / "download.json"
            source.write_text('{"installed":{"client_id":"test","client_secret":"mock-secret"}}')
            app = NinjaBlogApp()
            self.addCleanup(self._close_app, app)
            self.addCleanup(proceed.set)
            app.withdraw()
            app.update_idletasks()
            self.assertEqual(app.tabview.get(), "Tạo blog")
            app.tabview.set("Đăng ký tài khoản")
            self.assertEqual(app.tabview.get(), "Đăng ký tài khoản")
            tab = app.registration_tab
            self.assertEqual(tab.btn_login.cget("text"), "Đăng nhập")
            self.assertIsNotNone(tab.btn_stop_reg)
            self.assertIsNotNone(tab.btn_retry_mail)
            self.assertIsNotNone(app.btn_start)
            self.assertIsNotNone(app.btn_stop)
            tab.controller = Mock(spec=RegistrationWorkerController)
            tab._imap_factory = lambda *_: FakeImapConnection(state)
            tab.entry_inbox_user.insert(0, state.valid_user)
            tab.btn_login.invoke()
            self.assertEqual(tab.oauth_setup.winfo_manager(), "grid")
            google.assert_not_called()

            with patch("registration_tab.filedialog.askopenfilename", return_value=str(source)) as picker:
                tab._import_client()
                self.assertEqual(tab.btn_start_reg.cget("state"), "disabled")
                tab._login()  # A second click while Google is open must do nothing.
                proceed.set()
                kind, payload = tab.ui_queue.get(timeout=5)
                tab._handle_ui_event(kind, payload)
                google.assert_called_once()
                self.assertEqual(state.readonly_calls, [True])
                self.assertEqual(tab.lbl_connection.cget("text"), "Đã kết nối")
                self.assertEqual(tab.oauth_setup.winfo_manager(), "")
                tab.txt_reg_gmails.insert("1.0", state.valid_user)
                tab.entry_ninja_password.insert(0, "Ninja-pw_4")
                tab.btn_start_reg.invoke()
                config = tab.controller.start_batch.call_args.kwargs["config"]
                self.assertEqual(config.imap_config.oauth_token(), "mock-access-token")
                self.assertEqual(config.form.password, "Ninja-pw_4")
                self.assertNotIn("Ninja-pw_4", repr(config))
                self.assertEqual(config.max_sessions, 5)
                self.assertFalse(config.form.newsletter)
                self.assertTrue(config.form.accept_terms)
                self.assertTrue(config.form.accept_privacy)
                self.assertTrue(config.use_ocr)
                self.assertTrue(bool(config.ocr_url))
                google.assert_called_once()
                tab._set_controls_busy(False)
                self.assertEqual(tab.ui_queue.get_nowait()[0], "log")

                tab.entry_inbox_user.delete(0, "end")
                self.assertEqual(tab.lbl_connection.cget("text"), "Chưa đăng nhập")
                self.assertEqual(tab.btn_start_reg.cget("state"), "disabled")
                source.unlink()
                tab.entry_inbox_user.insert(0, state.valid_user)
                state.select_fails = True
                tab.btn_login.invoke()
                kind, payload = tab.ui_queue.get(timeout=5)
                tab._handle_ui_event(kind, payload)
                self.assertIn("INBOX", tab.lbl_connection.cget("text"))
                self.assertIsNone(tab._connected_mailbox)
                self.assertEqual(tab.btn_start_reg.cget("state"), "disabled")
                picker.assert_called_once()

    def test_cancel_login_unlocks_controls_and_ignores_stale_result(self):
        state = FakeImapServerState()
        gate = threading.Event()

        def slow_consent(**_kwargs):
            gate.wait(4)
            return Mock(token="mock-access-token", valid=True)

        with TemporaryDirectory() as folder, patch("gmail_fetcher.__file__", str(Path(folder) / "app.py")), \
             patch("gmail_fetcher._user_oauth_client_file", return_value=Path(folder) / "saved.json"), \
             patch("gmail_fetcher.InstalledAppFlow.run_local_server", side_effect=slow_consent), \
             patch("registration_tab.load_gmail_credentials", return_value=("", "")):
            saved = Path(folder) / "saved.json"
            saved.write_text('{"installed":{"client_id":"test","client_secret":"mock-secret"}}')
            app = NinjaBlogApp()
            self.addCleanup(self._close_app, app)
            self.addCleanup(gate.set)
            app.withdraw()
            app.update_idletasks()
            tab = app.registration_tab
            tab.controller = Mock(spec=RegistrationWorkerController)
            tab._imap_factory = lambda *_: FakeImapConnection(state)
            verify_done = threading.Event()
            orig_verify = tab._verify_login

            def tracked_verify(cfg):
                try:
                    orig_verify(cfg)
                finally:
                    verify_done.set()

            tab._verify_login = tracked_verify
            tab.entry_inbox_user.insert(0, state.valid_user)
            tab.btn_login.invoke()
            self.assertEqual(tab.btn_login.cget("state"), "disabled")
            self.assertEqual(tab.btn_cancel_login.winfo_manager(), "grid")

            tab.btn_cancel_login.invoke()
            self.assertEqual(tab.btn_login.cget("state"), "normal")
            self.assertEqual(tab.btn_cancel_login.winfo_manager(), "")
            self.assertEqual(tab.lbl_connection.cget("text"), "Đã hủy đăng nhập")
            self.assertIsNone(tab._login_pending)
            gate.set()
            self.assertTrue(verify_done.wait(timeout=4))
            tab.update_idletasks()
            self.assertTrue(tab.ui_queue.empty())
            self.assertIsNone(tab._connected_mailbox)

    def test_app_password_mode_connects_starts_and_never_leaks_secret(self):
        state = FakeImapServerState()
        state.valid_password = "abcdefghijklmnop"
        with patch("registration_tab.load_gmail_credentials", return_value=("", "")):
            app = NinjaBlogApp()
            self.addCleanup(self._close_app, app)
            app.withdraw()
            app.update_idletasks()
            tab = app.registration_tab
            tab.controller = Mock(spec=RegistrationWorkerController)
            tab._imap_factory = lambda *_: FakeImapConnection(state)

            tab.rb_auth_app_pw.invoke()
            self.assertEqual(tab.app_password_frame.winfo_manager(), "grid")
            self.assertEqual(tab.btn_login.cget("text"), "Kết nối")

            # Sai App Password -> báo lỗi, không cho bấm Bắt đầu đăng ký, không lộ mật khẩu
            tab.entry_inbox_user.insert(0, state.valid_user)
            tab.entry_app_password.insert(0, "zzzz yyyy xxxx wwww")
            tab.btn_login.invoke()
            kind, payload = tab.ui_queue.get(timeout=5)
            tab._handle_ui_event(kind, payload)
            self.assertNotIn("zzzzyyyyxxxxwwww", tab.lbl_connection.cget("text"))
            self.assertIn("App Password", tab.lbl_connection.cget("text"))
            self.assertIsNone(tab._connected_mailbox)
            self.assertEqual(tab.btn_start_reg.cget("state"), "disabled")

            # Đúng App Password có dấu cách nhóm 4 ký tự -> tự bỏ khoảng trắng và Đã kết nối
            tab.entry_app_password.delete(0, "end")
            tab.entry_app_password.insert(0, "abcd efgh ijkl mnop")
            self.assertEqual(tab.lbl_connection.cget("text"), "Chưa đăng nhập")
            tab.btn_login.invoke()
            kind, payload = tab.ui_queue.get(timeout=5)
            tab._handle_ui_event(kind, payload)
            self.assertEqual(tab.lbl_connection.cget("text"), "Đã kết nối")
            self.assertEqual(tab.btn_start_reg.cget("state"), "normal")

            address = "ro.otbox@gmail.com"
            tab.txt_reg_gmails.insert("1.0", address)
            with patch("registration_tab.messagebox.showwarning") as warning:
                tab.btn_start_reg.invoke()
                self.assertEqual(warning.call_args.args[0], "Mật khẩu Ninja không hợp lệ")
                tab.controller.start_batch.assert_not_called()

            tab.entry_ninja_password.insert(0, "Ninja-pw_5")
            self.assertEqual(tab.entry_ninja_password.cget("show"), "*")
            tab.chk_show_ninja_pw.toggle()
            self.assertEqual(tab.entry_ninja_password.cget("show"), "")
            tab.chk_show_ninja_pw.toggle()
            self.assertEqual(tab.entry_ninja_password.cget("show"), "*")
            tab.entry_max_sessions.delete(0, "end")
            tab.entry_max_sessions.insert(0, "11")
            with patch("registration_tab.messagebox.showwarning") as warning:
                tab.btn_start_reg.invoke()
                self.assertEqual(warning.call_args.args[0], "Số phiên không hợp lệ")
                tab.controller.start_batch.assert_not_called()

            tab.entry_max_sessions.delete(0, "end")
            tab.entry_max_sessions.insert(0, "2")
            tab.btn_start_reg.invoke()
            config = tab.controller.start_batch.call_args.kwargs["config"]
            self.assertEqual(config.imap_config.email, state.valid_user)
            self.assertEqual(config.imap_config.app_password, "abcdefghijklmnop")
            self.assertIsNone(config.imap_config.oauth_token)
            self.assertEqual(config.max_sessions, 2)
            self.assertFalse(config.form.newsletter)
            self.assertTrue(config.form.accept_terms)
            self.assertTrue(config.form.accept_privacy)

            tab._handle_ui_event("status", (1, address, STATUS_FORM_READY, "11:11:11"))
            row = tab.results_rows[1]
            _, open_button, accept_button = row["actions"]
            self.assertTrue(tab._is_running)
            self.assertEqual(tab.btn_start_reg.cget("state"), "disabled")
            self.assertEqual(open_button.cget("text"), "Mở tab")
            self.assertEqual(accept_button.cget("text"), "Đã tiếp nhận")

            open_button.invoke()
            tab.controller.focus_form.assert_called_once()
            self.assertEqual(tab.controller.focus_form.call_args.args, (1,))
            self.assertEqual(accept_button.cget("state"), "disabled")
            self.assertEqual(tab.ui_queue.get_nowait()[0], "log")
            tab.controller.focus_form.call_args.kwargs["on_finished"](None)
            tab._handle_ui_event(*tab.ui_queue.get_nowait())
            self.assertEqual(accept_button.cget("state"), "normal")

            accept_button.invoke()
            tab.controller.acknowledge_form.assert_called_once()
            self.assertEqual(tab.controller.acknowledge_form.call_args.args, (1,))
            tab._handle_ui_event("status", (1, address, STATUS_FORM_HANDOFF, "11:11:12"))
            self.assertNotIn("actions", row)
            self.assertTrue(tab._is_running)

    def test_cancelled_login_keeps_secrets_out_of_events(self):
        def cancelled():
            raise ValueError("mock-sensitive-redirect")

        cfg = ImapConfig(email="reader@example.test", oauth_token=cancelled)
        self.tab._imap_factory = Mock()
        RegistrationTab._verify_login(self.tab, cfg)
        kind, (user, error, needs_setup) = self.tab.ui_queue.get_nowait()
        self.assertEqual((kind, user, needs_setup), ("login_finished", cfg.email, False))
        self.assertTrue(error)
        self.assertNotIn("mock-sensitive-redirect", error)
        self.tab._imap_factory.assert_not_called()

    def test_retry_button_enabled_only_for_timeout_or_unknown_send(self):
        self.tab.results_rows = {
            1: {"email": "a.b@gmail.com", "status": STATUS_TIMEOUT, "updated_at": "10:00:00", "widgets": (MagicMock(), MagicMock())},
        }
        with patch("registration_tab.messagebox"):
            self.tab._finish_registration(None)
        self.tab.btn_retry_mail.configure.assert_called_with(state="normal")

        self.tab.btn_retry_mail.configure.reset_mock()
        self.tab.results_rows[1]["status"] = STATUS_CANCELLED
        with patch("registration_tab.messagebox"):
            self.tab._finish_registration(None)
        self.tab.btn_retry_mail.configure.assert_called_with(state="disabled")

        self.tab.btn_retry_mail.configure.reset_mock()
        self.tab.results_rows[1]["status"] = STATUS_SEND_UNKNOWN
        with patch("registration_tab.messagebox"):
            self.tab._finish_registration(None)
        self.tab.btn_retry_mail.configure.assert_called_with(state="normal")

    def test_stop_registration_controls_only_registration_controller(self):
        RegistrationTab._stop_registration(self.tab)
        self.tab.controller.request_stop.assert_called_once()
        self.tab.btn_stop_reg.configure.assert_called_once_with(state="disabled")

    def test_row_menu_only_copies_when_a_choice_is_selected(self):
        copied = []
        self.tab.results_rows = {
            1: {
                "email": "a.b@gmail.com",
                "status": STATUS_CONFIRMED,
                "updated_at": "10:15:00",
                "all_widgets": (),
            }
        }
        self.tab._flash_row = Mock()
        self.tab._copy_to_clipboard = lambda text, msg="": copied.append((text, msg))
        self.tab._copy_entire_row = lambda idx: RegistrationTab._copy_entire_row(self.tab, idx)
        with patch("registration_tab.show_row_menu") as menu:
            RegistrationTab._show_row_context_menu(self.tab, Mock(), 1)

        self.assertEqual(copied, [])
        entries = dict(menu.call_args.args[2])
        self.assertIn("Sao chép Gmail", entries)
        entries["Sao chép toàn bộ dòng #1"]()
        self.assertEqual(copied[0][0], "1\ta.b@gmail.com\tĐã nhận link xác nhận\t10:15:00")

    def test_export_csv_contains_only_successful_gmails_in_row_order(self):
        self.tab.results_rows = {
            4: {"email": "a.bc@gmail.com", "status": STATUS_REGISTERED},
            3: {"email": "ab.c@gmail.com", "status": STATUS_FORM_HANDOFF},
            2: {"email": "a.b.c@gmail.com", "status": STATUS_REGISTERED},
            1: {"email": "abc@gmail.com", "status": STATUS_CONFIRMED},
        }
        with TemporaryDirectory() as tmp:
            out_csv = Path(tmp) / "reg_results.csv"
            with patch("registration_tab.filedialog.asksaveasfilename", return_value=str(out_csv)), \
                    patch("registration_tab.messagebox"):
                RegistrationTab._export_csv(self.tab)
            with out_csv.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
        self.assertEqual(rows, [["Gmail"], ["a.b.c@gmail.com"], ["a.bc@gmail.com"]])

    def test_export_skips_save_dialog_when_no_registration_completed(self):
        self.tab.results_rows = {1: {"email": "abc@gmail.com", "status": STATUS_CONFIRMED}}
        with patch("registration_tab.filedialog.asksaveasfilename") as dialog, patch("registration_tab.messagebox"):
            RegistrationTab._export_csv(self.tab)
        dialog.assert_not_called()

    def test_101_gmails_share_password_and_apply_registration_defaults(self):
        state = FakeImapServerState()
        state.valid_user = "abcdefghij@gmail.com"
        state.valid_password = "abcdefghijklmnop"
        # Tạo 101 biến thể dấu chấm hợp lệ từ 10 ký tự local (có 2^9 = 512 cách chèn dấu chấm)
        variants = []
        local = "abcdefghij"
        for mask in range(1, 102):
            dotted = local[0]
            for bit in range(len(local) - 1):
                if mask & (1 << bit):
                    dotted += "."
                dotted += local[bit + 1]
            variants.append(f"{dotted}@gmail.com")

        with patch("registration_tab.load_gmail_credentials", return_value=("", "")):
            app = NinjaBlogApp()
            self.addCleanup(self._close_app, app)
            app.withdraw()
            app.update_idletasks()
            tab = app.registration_tab
            tab.controller = Mock(spec=RegistrationWorkerController)
            tab._imap_factory = lambda *_: FakeImapConnection(state)

            tab.rb_auth_app_pw.invoke()
            tab.entry_inbox_user.insert(0, state.valid_user)
            tab.entry_app_password.insert(0, state.valid_password)
            tab.btn_login.invoke()
            kind, payload = tab.ui_queue.get(timeout=5)
            tab._handle_ui_event(kind, payload)
            self.assertEqual(tab.lbl_connection.cget("text"), "Đã kết nối")

            tab.txt_reg_gmails.insert("1.0", "\n".join(variants))
            tab._update_gmail_counter()
            self.assertEqual(tab.lbl_reg_gmail_count.cget("text"), "101 / 101")

            tab.entry_ninja_password.insert(0, "Shared_Pass99")
            tab.btn_start_reg.invoke()

            config = tab.controller.start_batch.call_args.kwargs["config"]
            self.assertEqual([e.email for e in config.entries], variants)
            self.assertEqual(config.form.password, "Shared_Pass99")
            self.assertFalse(hasattr(tab, "chk_newsletter"))
            self.assertFalse(hasattr(tab, "chk_accept_terms"))
            self.assertFalse(hasattr(tab, "chk_accept_privacy"))
            self.assertFalse(config.form.newsletter)
            self.assertTrue(config.form.accept_terms)
            self.assertTrue(config.form.accept_privacy)
            self.assertTrue(config.use_ocr)
            self.assertEqual(len(tab.results_rows), 101)

    def test_registered_status_counts_as_success_and_reads_env_ocr_url(self):
        self.tab.results_rows = {
            1: {
                "email": "a.b@gmail.com",
                "status": STATUS_REGISTERED,
                "updated_at": "10:00:00",
                "widgets": (MagicMock(), MagicMock()),
            }
        }
        with patch("registration_tab.messagebox") as mb:
            self.tab._finish_registration(None)
        mb.showinfo.assert_called_once()
        mb.showwarning.assert_not_called()
        self.assertIn("Hoàn tất đăng ký · 1", mb.showinfo.call_args.args[1])

        with TemporaryDirectory() as tmp:
            env_file = Path(tmp) / ".env"
            env_file.write_text("OCR_HIRAGANA_URL=http://127.0.0.1:8123/\n", encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(resolve_default_ocr_url(env_file=env_file), "http://127.0.0.1:8123")


if __name__ == "__main__":
    unittest.main()
