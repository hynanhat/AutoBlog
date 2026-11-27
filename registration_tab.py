"""
Tab Đăng ký tài khoản trên giao diện Desktop (CustomTkinter):
- Đăng nhập Google và kiểm tra hộp thư trước khi bắt đầu phiên đăng ký
- Cập nhật giao diện qua hàng đợi sự kiện (batching như app.py)
- Xuất Gmail của các tài khoản đã hoàn tất đăng ký.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox
from time import perf_counter
from typing import Any, Callable, Dict, Optional

import customtkinter as ctk

from gmail_fetcher import (
    GmailFetcherError,
    GmailRegistrationReader,
    GoogleMailboxLogin,
    ImapConfig,
    OAuthClientConfigError,
    configured_oauth_client,
    install_oauth_client,
    STATUS_CHECK_MAIL,
    STATUS_CONFIRMED,
    load_gmail_credentials,
    normalize_app_password,
)
from ocr_client import resolve_default_ocr_url
from parsers import parse_gmails, parse_registration_gmails
from registration_form import RegistrationFormConfig, validate_registration_password
from registration_worker import (
    DEFAULT_WAIT_TIMEOUT_SECONDS,
    RETRYABLE_STATUSES,
    RegistrationConfig,
    RegistrationWorkerController,
    STATUS_CANCELLED,
    STATUS_EMAIL_EXISTS,
    STATUS_FAILED,
    STATUS_FORM_HANDOFF,
    STATUS_FORM_READY,
    STATUS_NOT_SUBMITTED,
    STATUS_PENDING,
    STATUS_REGISTERED,
    STATUS_SEND_UNKNOWN,
    STATUS_TAB_CLOSED,
    STATUS_TIMEOUT,
)
from ui_style import (
    ACCENT,
    BORDER,
    INK,
    MUTED,
    SELECTED,
    STRIPE,
    SURFACE,
    TABLE_HEADER,
    bind_row_menu,
    card,
    empty_state,
    log_panel,
    primary_button,
    scroll_panel,
    secondary_button,
    section_title,
    show_row_menu,
    stop_button,
    textbox,
)


class RegistrationTab(ctk.CTkFrame):
    """Gửi đăng ký tạm, lấy link xác nhận và bàn giao form Ninja."""

    def __init__(
        self,
        master: Any,
        controller: Optional[RegistrationWorkerController] = None,
        imap_factory: Optional[Callable[[str, int, float], Any]] = None,
        *,
        toolbar_parent: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(master, fg_color="transparent", **kwargs)
        self.controller = controller or RegistrationWorkerController()
        self._imap_factory = imap_factory
        self._login_pending: Optional[ImapConfig] = None
        self._cancelled_login_ids: set[int] = set()
        self._connected_mailbox: Optional[ImapConfig] = None
        self._batch_email: Optional[str] = None
        env_email, env_app_password = load_gmail_credentials()
        self._inbox_email = tk.StringVar(self, value=env_email)
        self._app_password = tk.StringVar(self, value=env_app_password)
        self._show_app_password = tk.BooleanVar(self, value=False)
        self._show_ninja_password = tk.BooleanVar(self, value=False)
        self._auth_mode = tk.StringVar(
            self, value="app_password" if env_app_password else "oauth"
        )
        self.ui_queue: queue.Queue = queue.Queue()
        self.results_rows: Dict[int, Dict[str, Any]] = {}
        self._after_id: Optional[str] = None
        self._is_running = False
        self._is_closing = False

        self._build_layout(toolbar_parent)
        self._inbox_email.trace_add("write", self._mailbox_edited)
        self._app_password.trace_add("write", self._mailbox_edited)
        self._on_auth_mode_changed()
        self._after_id = self.after(150, self._poll_ui_queue)

    def _build_layout(self, toolbar_parent: Any) -> None:
        self.grid_columnconfigure(0, weight=4, minsize=365)
        self.grid_columnconfigure(1, weight=6, minsize=570)
        content_row = 1 if toolbar_parent is None else 0
        self.grid_rowconfigure(content_row, weight=1)

        self.toolbar = toolbar_parent
        if toolbar_parent is None:
            self.toolbar = ctk.CTkFrame(self, fg_color="transparent")
            self.toolbar.grid(row=0, column=0, columnspan=2, sticky="e", pady=(0, 12))
        self.btn_export_reg_csv = secondary_button(
            self.toolbar, text="Xuất CSV", width=104, command=self._export_csv,
        )
        self.btn_export_reg_csv.pack(side="left", padx=(0, 8))
        self.btn_stop_reg = stop_button(
            self.toolbar, text="Dừng đăng ký", width=120, state="disabled", command=self._stop_registration,
        )
        self.btn_stop_reg.pack(side="left", padx=(0, 8))
        self.btn_start_reg = primary_button(
            self.toolbar, text="Bắt đầu đăng ký", width=145, command=self._start_registration,
        )
        self.btn_start_reg.pack(side="left")

        # ================= CỘT TRÁI: CẤU HÌNH ĐĂNG KÝ =================
        left = scroll_panel(self, label_text="Cấu hình đăng ký")
        left.grid(row=content_row, column=0, padx=(0, 7), sticky="nsew")
        left.grid_columnconfigure(0, weight=1)

        gmail_header = ctk.CTkFrame(left, fg_color="transparent")
        gmail_header.grid(row=0, column=0, padx=10, pady=(4, 3), sticky="ew")
        gmail_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(gmail_header, text="Danh sách Gmail đăng ký", anchor="w", text_color=INK,
                     font=("Segoe UI", 14, "bold")).grid(row=0, column=0, sticky="w")
        self.lbl_reg_gmail_count = ctk.CTkLabel(
            gmail_header, text="0 / 101", anchor="e", text_color=MUTED, font=("Segoe UI", 13, "bold"),
        )
        self.lbl_reg_gmail_count.grid(row=0, column=1, sticky="e")

        self.txt_reg_gmails = textbox(left, height=146, placeholder="Mỗi dòng một email")
        self.txt_reg_gmails.grid(row=1, column=0, padx=10, pady=(0, 4), sticky="ew")
        self.txt_reg_gmails.bind("<KeyRelease>", self._update_gmail_counter)
        self.txt_reg_gmails.bind("<<Paste>>", lambda _e: self.after_idle(self._update_gmail_counter))

        ctk.CTkLabel(
            left,
            text="Giữ nguyên vị trí dấu chấm trong địa chỉ.",
            anchor="w",
            text_color=MUTED,
            wraplength=350,
        ).grid(row=2, column=0, padx=10, pady=(0, 6), sticky="ew")

        mailbox_header = ctk.CTkFrame(left, fg_color="transparent")
        mailbox_header.grid(row=3, column=0, padx=10, pady=(0, 4), sticky="ew")
        ctk.CTkFrame(mailbox_header, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            mailbox_header, text="Gmail nhận thư xác nhận", anchor="w", text_color=INK,
            font=("Segoe UI", 14, "bold"),
        ).pack(fill="x")

        auth_mode_row = ctk.CTkFrame(left, fg_color="transparent")
        auth_mode_row.grid(row=4, column=0, padx=10, pady=(0, 4), sticky="ew")
        self.rb_auth_oauth = ctk.CTkRadioButton(
            auth_mode_row,
            text="Đăng nhập Google",
            fg_color=ACCENT,
            variable=self._auth_mode,
            value="oauth",
            command=self._on_auth_mode_changed,
        )
        self.rb_auth_oauth.pack(side="left", padx=(0, 12))
        self.rb_auth_app_pw = ctk.CTkRadioButton(
            auth_mode_row,
            text="App Password (16 ký tự)",
            fg_color=ACCENT,
            variable=self._auth_mode,
            value="app_password",
            command=self._on_auth_mode_changed,
        )
        self.rb_auth_app_pw.pack(side="left")

        mailbox_row = ctk.CTkFrame(left, fg_color="transparent")
        mailbox_row.grid(row=5, column=0, padx=10, sticky="ew")
        mailbox_row.grid_columnconfigure(0, weight=1)
        self.entry_inbox_user = ctk.CTkEntry(
            mailbox_row, textvariable=self._inbox_email, height=34, border_width=1, border_color=BORDER,
        )
        self.entry_inbox_user.grid(row=0, column=0, padx=(0, 6), sticky="ew")
        self.entry_inbox_user.bind("<Return>", lambda _event: self._login())
        self.btn_login = secondary_button(mailbox_row, text="Đăng nhập", width=96, command=self._login)
        self.btn_login.grid(row=0, column=1)
        self.btn_cancel_login = stop_button(
            mailbox_row,
            text="Hủy",
            width=56,
            command=self._cancel_login,
        )
        self.btn_cancel_login.grid(row=0, column=2, padx=(6, 0))
        self.btn_cancel_login.grid_remove()

        self.app_password_frame = ctk.CTkFrame(left, fg_color="transparent")
        self.app_password_frame.grid(row=6, column=0, padx=10, pady=(6, 2), sticky="ew")
        self.app_password_frame.grid_columnconfigure(0, weight=1)

        pw_header = ctk.CTkFrame(self.app_password_frame, fg_color="transparent")
        pw_header.grid(row=0, column=0, columnspan=2, sticky="ew")
        ctk.CTkLabel(pw_header, text="App Password (16 ký tự):", anchor="w").pack(side="left")
        self.chk_show_app_pw = ctk.CTkCheckBox(
            pw_header,
            text="Hiện",
            variable=self._show_app_password,
            command=self._toggle_show_app_password,
            width=60,
        )
        self.chk_show_app_pw.pack(side="right")

        self.entry_app_password = ctk.CTkEntry(
            self.app_password_frame,
            show="*",
            textvariable=self._app_password,
            height=34, border_width=1, border_color=BORDER,
        )
        self.entry_app_password.grid(row=1, column=0, padx=(0, 6), pady=(2, 2), sticky="ew")
        self.entry_app_password.bind("<Return>", lambda _event: self._login())
        self.btn_get_app_password = secondary_button(
            self.app_password_frame,
            text="Lấy App Password",
            width=128,
            command=lambda: webbrowser.open("https://myaccount.google.com/apppasswords"),
        )
        self.btn_get_app_password.grid(row=1, column=1, pady=(2, 2))
        ctk.CTkLabel(
            self.app_password_frame,
            text="Bật xác minh 2 bước trên Gmail → bấm Lấy App Password → tạo tên bất kỳ và dán mã 16 ký tự vào đây.",
            anchor="w",
            justify="left",
            wraplength=350,
            text_color=MUTED,
        ).grid(row=2, column=0, columnspan=2, pady=(2, 0), sticky="ew")
        self.app_password_frame.grid_remove()

        self.lbl_connection = ctk.CTkLabel(
            left, text="Chưa đăng nhập", anchor="w", justify="left", wraplength=350, text_color=MUTED,
        )
        self.lbl_connection.grid(row=7, column=0, padx=10, sticky="ew")

        self.oauth_setup = ctk.CTkFrame(left, fg_color="transparent")
        self.oauth_setup.grid(row=8, column=0, padx=10, pady=(0, 6), sticky="ew")
        ctk.CTkLabel(
            self.oauth_setup, anchor="w", justify="left", wraplength=350,
            text="Thiết lập một lần cho AutoBlog. Tạo OAuth client loại Desktop app rồi chọn tệp JSON đã tải.",
        ).pack(fill="x")
        secondary_button(
            self.oauth_setup, text="Tạo OAuth Desktop", width=140,
            command=lambda: webbrowser.open("https://console.cloud.google.com/auth/clients"),
        ).pack(side="left", padx=(0, 6))
        secondary_button(self.oauth_setup, text="Chọn tệp JSON", width=120, command=self._import_client).pack(side="left")
        self.oauth_setup.grid_remove()

        row_timeout = ctk.CTkFrame(left, fg_color="transparent")
        row_timeout.grid(row=9, column=0, padx=10, pady=(2, 8), sticky="ew")
        row_timeout.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(row_timeout, text="Thời gian chờ thư (giây)", anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self.entry_wait_timeout = ctk.CTkEntry(row_timeout, width=90, height=34, border_width=1, border_color=BORDER)
        self.entry_wait_timeout.insert(0, str(int(DEFAULT_WAIT_TIMEOUT_SECONDS)))
        self.entry_wait_timeout.grid(row=0, column=1, padx=(8, 0), sticky="e")

        ninja_header = ctk.CTkFrame(left, fg_color="transparent")
        ninja_header.grid(row=10, column=0, padx=10, pady=(4, 2), sticky="ew")
        ctk.CTkFrame(ninja_header, fg_color=BORDER, height=1, corner_radius=0).pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            ninja_header, text="Thiết lập tài khoản Ninja", anchor="w", text_color=INK,
            font=("Segoe UI", 14, "bold"),
        ).pack(fill="x")

        form_options = ctk.CTkFrame(left, fg_color="transparent")
        form_options.grid(row=11, column=0, padx=10, pady=(4, 8), sticky="ew")
        form_options.grid_columnconfigure(0, weight=1)
        form_options.grid_columnconfigure(1, weight=1)
        ninja_pw_header = ctk.CTkFrame(form_options, fg_color="transparent")
        ninja_pw_header.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkLabel(
            ninja_pw_header, text="Mật khẩu đăng ký", anchor="w",
        ).pack(side="left")
        self.chk_show_ninja_pw = ctk.CTkCheckBox(
            ninja_pw_header,
            text="Hiện",
            variable=self._show_ninja_password,
            command=self._toggle_show_ninja_password,
            width=52,
            checkbox_width=17,
            checkbox_height=17,
        )
        self.chk_show_ninja_pw.pack(side="right")
        self.entry_ninja_password = ctk.CTkEntry(
            form_options, show="*", placeholder_text="4–16 ký tự", height=34,
            border_width=1, border_color=BORDER,
        )
        self.entry_ninja_password.grid(row=1, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkLabel(form_options, text="Số phiên đồng thời (1–10)", anchor="w").grid(
            row=0, column=1, sticky="w"
        )
        self.entry_max_sessions = ctk.CTkEntry(form_options, width=130, height=34, border_width=1, border_color=BORDER)
        self.entry_max_sessions.insert(0, "5")
        self.entry_max_sessions.grid(row=1, column=1, sticky="ew")

        # ================= CỘT PHẢI: ĐIỀU KHIỂN & KẾT QUẢ =================
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=content_row, column=1, padx=(7, 0), sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(0, weight=3, minsize=235)
        right.grid_rowconfigure(1, weight=2, minsize=145)

        progress = card(right)
        progress.grid(row=0, column=0, sticky="nsew", pady=(0, 12))
        progress.grid_columnconfigure(0, weight=1)
        progress.grid_rowconfigure(1, weight=1)
        progress_header = ctk.CTkFrame(progress, fg_color="transparent")
        progress_header.grid(row=0, column=0, padx=14, pady=(10, 8), sticky="ew")
        section_title(progress_header, "Tiến độ đăng ký").pack(side="left")
        self.btn_retry_mail = secondary_button(
            progress_header, text="Kiểm tra thư lại", width=126, state="disabled", command=self._retry_mail_check,
        )
        self.btn_retry_mail.pack(side="right")

        table = card(progress)
        table.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        self.table_frame = ctk.CTkScrollableFrame(
            table, fg_color=SURFACE, corner_radius=7, scrollbar_button_color=BORDER,
        )
        self.table_frame.grid(row=0, column=0, padx=1, pady=1, sticky="nsew")
        self.table_frame.grid_columnconfigure(1, weight=2)
        self.table_frame.grid_columnconfigure(2, weight=2)
        self.table_frame.grid_columnconfigure(3, weight=1)
        self._render_table_header()

        self.empty_results = empty_state(table, "Chưa có kết quả", "Thêm Gmail và bấm Bắt đầu đăng ký.")
        self.empty_results.place(relx=0.5, rely=0.55, anchor="center")

        activity, self.txt_log = log_panel(right)
        activity.grid(row=1, column=0, sticky="nsew")

    def _render_table_header(self) -> None:
        headers = [("#", 35), ("Gmail", 0), ("Trạng thái", 0), ("Thời điểm", 85), ("Thao tác", 148)]
        for col, (text, width) in enumerate(headers):
            lbl = ctk.CTkLabel(
                self.table_frame,
                text=text,
                width=width,
                height=34,
                anchor="w" if col in (1, 2) else "center",
                padx=8,
                fg_color=TABLE_HEADER,
                text_color=INK,
                font=("Segoe UI", 12, "bold"),
            )
            lbl.grid(row=0, column=col, sticky="ew")

    def _update_gmail_counter(self, _event=None) -> None:
        gmails, _ = parse_gmails(self.txt_reg_gmails.get("1.0", "end"))
        self.lbl_reg_gmail_count.configure(text=f"{len(gmails)} / 101")

    def _mailbox_edited(self, *_args) -> None:
        self._connected_mailbox = None
        self.lbl_connection.configure(text="Chưa đăng nhập")
        self._sync_controls()

    def _toggle_show_app_password(self) -> None:
        entry = getattr(self, "entry_app_password", None)
        if entry is not None:
            entry.configure(show="" if self._show_app_password.get() else "*")

    def _toggle_show_ninja_password(self) -> None:
        entry = getattr(self, "entry_ninja_password", None)
        if entry is not None:
            entry.configure(show="" if self._show_ninja_password.get() else "*")

    def _on_auth_mode_changed(self) -> None:
        if self._login_pending is not None or self._is_running:
            return
        self._connected_mailbox = None
        self.lbl_connection.configure(text="Chưa đăng nhập")
        mode = self._auth_mode.get() if hasattr(self, "_auth_mode") else "oauth"
        if mode == "app_password":
            self.oauth_setup.grid_remove()
            self.app_password_frame.grid()
        else:
            self.app_password_frame.grid_remove()
        self._sync_controls()

    def _import_client(self) -> None:
        if self._login_pending or self._is_running:
            return
        filename = filedialog.askopenfilename(
            parent=self, title="Chọn tệp OAuth Desktop của AutoBlog",
            filetypes=[("Google OAuth JSON", "*.json")],
        )
        if not filename:
            return
        try:
            install_oauth_client(Path(filename))
        except OAuthClientConfigError as exc:
            self.lbl_connection.configure(text=str(exc))
            return
        except OSError:
            self.lbl_connection.configure(text="Không lưu được cấu hình AutoBlog trên máy này.")
            return
        self._login()

    def _sync_controls(self) -> None:
        busy = self._is_running or self._login_pending is not None
        has_ready_form = any(row["status"] == STATUS_FORM_READY for row in self.results_rows.values())
        connected = self._connected_mailbox
        auth_mode_var = getattr(self, "_auth_mode", None)
        mode = auth_mode_var.get() if auth_mode_var is not None else "oauth"
        is_app_pw = mode == "app_password"
        self.entry_inbox_user.configure(state="disabled" if busy else "normal")
        self.btn_login.configure(
            text="Kết nối" if is_app_pw else "Đăng nhập",
            state="disabled" if busy else "normal",
        )
        for attr in (
            "rb_auth_oauth", "rb_auth_app_pw", "entry_app_password", "btn_get_app_password", "chk_show_app_pw",
            "entry_ninja_password", "chk_show_ninja_pw", "entry_max_sessions",
        ):
            widget = getattr(self, attr, None)
            if widget is not None:
                widget.configure(state="disabled" if busy else "normal")
        cancel_btn = getattr(self, "btn_cancel_login", None)
        if cancel_btn is not None:
            if self._login_pending is not None and not self._is_running:
                cancel_btn.grid()
            else:
                cancel_btn.grid_remove()
        self.btn_start_reg.configure(state="normal" if connected and not busy and not has_ready_form else "disabled")
        self.btn_stop_reg.configure(state="normal" if self._is_running else "disabled")
        can_retry = (
            not busy and connected and connected.email == self._batch_email and
            any(row.get("status") in RETRYABLE_STATUSES for row in self.results_rows.values())
        )
        self.btn_retry_mail.configure(state="normal" if can_retry else "disabled")

    def _set_controls_busy(self, busy: bool) -> None:
        self._is_running = busy
        self._sync_controls()

    def _start_login_check(self, cfg: ImapConfig, status_text: str) -> None:
        self._login_pending = cfg
        self.lbl_connection.configure(text=status_text)
        self._sync_controls()
        threading.Thread(target=self._verify_login, args=(cfg,), daemon=True).start()

    def _login(self) -> None:
        if self._login_pending or self._is_running or self._is_closing:
            return
        email = self._inbox_email.get().strip().lower()
        _, errors = parse_registration_gmails("", root_gmail=email)
        if errors:
            self.lbl_connection.configure(text="Nhập địa chỉ Gmail nhận thư xác nhận.")
            self.entry_inbox_user.focus_set()
            return
        self._connected_mailbox = None
        auth_mode_var = getattr(self, "_auth_mode", None)
        mode = auth_mode_var.get() if auth_mode_var is not None else "oauth"
        if mode == "app_password":
            raw_pw = self._app_password.get() if hasattr(self, "_app_password") else ""
            app_password = normalize_app_password(raw_pw)
            if not app_password:
                self.lbl_connection.configure(text="Nhập App Password 16 ký tự của Gmail.")
                entry_pw = getattr(self, "entry_app_password", None)
                if entry_pw is not None:
                    entry_pw.focus_set()
                self._sync_controls()
                return
            self.oauth_setup.grid_remove()
            self._start_login_check(
                ImapConfig(email=email, app_password=app_password),
                "Đang kiểm tra kết nối IMAP…",
            )
            return

        client_file = configured_oauth_client()
        if client_file is None:
            self.lbl_connection.configure(text="AutoBlog cần cấu hình đăng nhập Google lần đầu.")
            self.oauth_setup.grid()
            self._sync_controls()
            return
        self.oauth_setup.grid_remove()
        login = GoogleMailboxLogin(email, client_file)
        self._start_login_check(
            ImapConfig(email=email, oauth_token=login.token),
            "Đang đăng nhập qua Google…",
        )

    def _cancel_login(self) -> None:
        pending = self._login_pending
        if pending is None:
            return
        self._cancelled_login_ids.add(id(pending))
        self._login_pending = None
        self.lbl_connection.configure(text="Đã hủy đăng nhập")
        self._sync_controls()

    def _verify_login(self, config: ImapConfig) -> None:
        error = None
        needs_setup = False
        try:
            GmailRegistrationReader(config, imap_factory=self._imap_factory).verify_connection()
        except OAuthClientConfigError as exc:
            error, needs_setup = str(exc), True
        except GmailFetcherError as exc:
            error = str(exc)
        except Exception:
            error = "Không thể kết nối hộp thư. Vui lòng thử đăng nhập lại."
        cancelled = getattr(self, "_cancelled_login_ids", None)
        if isinstance(cancelled, set) and id(config) in cancelled:
            return
        self.ui_queue.put(("login_finished", (config.email, error, needs_setup)))

    def _finish_login(self, email: str, error: Optional[str], needs_setup: bool) -> None:
        pending = self._login_pending
        if self._is_closing or pending is None or pending.email != email:
            return
        self._login_pending = None
        if self._inbox_email.get().strip().lower() == email:
            if error:
                self.lbl_connection.configure(text=error)
                if needs_setup:
                    self.oauth_setup.grid()
            else:
                self._connected_mailbox = pending
                self.lbl_connection.configure(text="Đã kết nối")
        self._sync_controls()

    def _start_registration(self) -> None:
        if self._is_running or any(row["status"] == STATUS_FORM_READY for row in self.results_rows.values()):
            return
        cfg = self._connected_mailbox
        if cfg is None or self._login_pending or cfg.email != self._inbox_email.get().strip().lower():
            return

        try:
            wait_timeout = float(self.entry_wait_timeout.get().strip())
        except ValueError:
            messagebox.showwarning(
                "Thời gian chờ không hợp lệ",
                "Vui lòng nhập thời gian chờ thư là số giây lớn hơn 0.",
            )
            return
        if wait_timeout <= 0 or wait_timeout > 86400:
            messagebox.showwarning(
                "Thời gian chờ không hợp lệ",
                "Thời gian chờ thư phải nằm trong khoảng từ 1 đến 86400 giây.",
            )
            return

        entries, errors = parse_registration_gmails(
            self.txt_reg_gmails.get("1.0", "end"), root_gmail=cfg.email
        )
        if errors:
            messagebox.showerror("Lỗi danh sách Gmail đăng ký", "\n".join(errors[:15]))
            return
        if not entries:
            messagebox.showwarning(
                "Thiếu dữ liệu", "Vui lòng nhập ít nhất 1 địa chỉ Gmail đăng ký."
            )
            return

        password = self.entry_ninja_password.get()
        if not validate_registration_password(password):
            messagebox.showwarning(
                "Mật khẩu Ninja không hợp lệ",
                "Nhập 4–16 ký tự gồm A–Z, a–z, 0–9 và các dấu . _ -.",
            )
            self.entry_ninja_password.focus_set()
            return

        try:
            max_sessions = int(self.entry_max_sessions.get().strip())
        except ValueError:
            max_sessions = 0
        if not 1 <= max_sessions <= 10:
            messagebox.showwarning("Số phiên không hợp lệ", "Nhập số phiên đồng thời từ 1 đến 10.")
            self.entry_max_sessions.focus_set()
            return

        form = RegistrationFormConfig(
            password=password,
            newsletter=False,
            accept_terms=True,
            accept_privacy=True,
        )
        self.empty_results.place_forget()
        for widget in self.table_frame.winfo_children():
            widget.destroy()
        self._render_table_header()
        self.results_rows.clear()
        for idx, entry in enumerate(entries, start=1):
            self._create_row(idx, entry.email)

        config = RegistrationConfig(
            entries=entries,
            imap_config=cfg,
            wait_timeout_seconds=wait_timeout,
            form=form,
            max_sessions=max_sessions,
            use_ocr=True,
            ocr_url=resolve_default_ocr_url(),
        )

        self._batch_email = cfg.email
        self._set_controls_busy(True)
        self.ui_queue.put(("log", f"Bắt đầu phiên đăng ký cho {len(entries)} Gmail…"))
        self.controller.start_batch(
            config=config,
            on_status=lambda i, e, s, t: self.ui_queue.put(("status", (i, e, s, t))),
            on_log=lambda msg: self.ui_queue.put(("log", msg)),
            on_finished=lambda err: self.ui_queue.put(("reg_finished", err)),
            imap_factory=self._imap_factory,
        )

    def _stop_registration(self) -> None:
        self.btn_stop_reg.configure(state="disabled")
        self.ui_queue.put(("log", "Đã nhận lệnh Dừng đăng ký. Đang đánh thức bộ đọc và dừng phiên…"))
        self.controller.request_stop()

    def _retry_mail_check(self) -> None:
        if (self._is_running or self._login_pending or self._connected_mailbox is None
                or self._connected_mailbox.email != self._batch_email):
            return
        self._set_controls_busy(True)
        self.ui_queue.put(("log", "Đang kiểm tra thư lại từ mốc hộp thư đã lưu…"))
        self.controller.retry_mail_check(
            on_finished=lambda err, changed: self.ui_queue.put(
                ("retry_finished", (err, changed))
            )
        )

    def _open_confirmed_form(self, stt: int) -> None:
        if self._is_running or self._login_pending:
            messagebox.showinfo("Đang chạy", "Vui lòng đợi tác vụ hiện tại hoàn tất.")
            return
        self._set_controls_busy(True)
        self.controller.open_confirmation_page(
            stt,
            on_finished=lambda err: self.ui_queue.put(("open_finished", (stt, err))),
        )

    def _render_form_actions(self, stt: int, row: Dict[str, Any]) -> None:
        actions = row.get("actions")
        if row["status"] != STATUS_FORM_READY:
            if actions is not None:
                actions[0].destroy()
                del row["actions"]
            return
        if actions is not None:
            return
        frame = ctk.CTkFrame(self.table_frame, fg_color="transparent")
        frame.grid(row=stt, column=4, padx=4, pady=2)
        open_button = secondary_button(
            frame, text="Mở tab", width=52, height=27,
            command=lambda: self._form_action(stt, acknowledge=False),
        )
        open_button.pack(side="left", padx=(0, 4))
        accept_button = primary_button(
            frame, text="Đã tiếp nhận", width=92, height=27,
            command=lambda: self._form_action(stt, acknowledge=True),
        )
        accept_button.pack(side="left")
        row["actions"] = frame, open_button, accept_button

    def _form_action(self, stt: int, *, acknowledge: bool) -> None:
        row = self.results_rows.get(stt)
        if row is None or row["status"] != STATUS_FORM_READY:
            return
        action = self.controller.acknowledge_form if acknowledge else self.controller.focus_form
        for button in row["actions"][1:]:
            button.configure(state="disabled")

        def finished(err: Optional[str]) -> None:
            self.ui_queue.put(("form_action_finished", (stt, err)))

        try:
            action(stt, on_finished=finished)
        except RuntimeError:
            finished("Phiên trình duyệt của dòng này không còn sẵn sàng.")

    def _create_row(self, idx: int, email: str) -> None:
        row_color = STRIPE if idx % 2 == 0 else SURFACE
        cell_style = dict(height=34, fg_color=row_color, text_color=INK, cursor="hand2")
        lbl_idx = ctk.CTkLabel(
            self.table_frame,
            text=str(idx),
            width=35,
            **cell_style,
        )
        lbl_mail = ctk.CTkLabel(
            self.table_frame,
            text=email,
            anchor="w",
            justify="left",
            padx=8,
            wraplength=145,
            **cell_style,
        )
        lbl_status = ctk.CTkLabel(
            self.table_frame,
            text=STATUS_PENDING,
            anchor="w",
            justify="left",
            padx=8,
            wraplength=140,
            **cell_style,
        )
        lbl_time = ctk.CTkLabel(
            self.table_frame,
            text="-",
            width=85,
            **cell_style,
        )
        action_cell = ctk.CTkLabel(self.table_frame, text="", width=148, **cell_style)

        all_widgets = (lbl_idx, lbl_mail, lbl_status, lbl_time, action_cell)
        for column, widget in enumerate(all_widgets):
            widget.grid(row=idx, column=column, sticky="nsew")
        self.results_rows[idx] = {
            "email": email,
            "status": STATUS_PENDING,
            "updated_at": "-",
            "widgets": (lbl_status, lbl_time),
            "all_widgets": all_widgets,
        }
        bind_row_menu(all_widgets, lambda event: self._show_row_context_menu(event, idx))

    def _copy_to_clipboard(self, text: str, message: str = "") -> None:
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            if message:
                self.ui_queue.put(("log", f"[Đã sao chép] {message}"))
        except Exception as exc:
            self.ui_queue.put(("log", f"Lỗi sao chép: {exc}"))

    def _flash_row(self, idx: int) -> None:
        row = self.results_rows.get(idx)
        if not row:
            return
        all_w = row.get("all_widgets", ())
        for w in all_w:
            w.configure(fg_color=SELECTED)

        def reset_colors() -> None:
            row_color = STRIPE if idx % 2 == 0 else SURFACE
            for widget in all_w:
                if widget.winfo_exists():
                    widget.configure(fg_color=row_color)

        self.after(250, reset_colors)

    def _copy_entire_row(self, idx: int) -> None:
        row = self.results_rows.get(idx)
        if not row:
            return
        self._flash_row(idx)
        text = f"{idx}\t{row.get('email', '')}\t{row.get('status', '')}\t{row.get('updated_at', '-')}".rstrip()
        self._copy_to_clipboard(text, f"Dòng #{idx}")

    def _copy_email_cell(self, idx: int) -> None:
        row = self.results_rows.get(idx)
        if not row:
            return
        val = (row.get("email") or "").strip()
        if not val:
            return
        self._flash_row(idx)
        self._copy_to_clipboard(val, f"Gmail #{idx}")

    def _show_row_context_menu(self, event, idx: int) -> None:
        row = self.results_rows.get(idx)
        if not row:
            return
        status = row.get("status", "")
        entries = [
            ("Sao chép Gmail", lambda: self._copy_email_cell(idx)),
            (f"Sao chép toàn bộ dòng #{idx}", lambda: self._copy_entire_row(idx)),
        ]
        if status == STATUS_CONFIRMED and not self._is_running:
            entries.append(("Mở form đăng ký", lambda: self._open_confirmed_form(idx)))
        show_row_menu(self, event, entries)

    def _handle_ui_event(self, kind: str, payload: Any) -> None:
        if kind == "status":
            idx, email, status, updated_at = payload
            row = self.results_rows.get(idx)
            if row and row["email"] == email:
                row["status"] = status
                row["updated_at"] = updated_at or "-"
                lbl_status, lbl_time = row["widgets"]
                lbl_status.configure(text=status)
                lbl_time.configure(text=row["updated_at"])
                self._render_form_actions(idx, row)
                if not self._is_running:
                    self._sync_controls()
        elif kind == "info":
            title, msg = payload
            messagebox.showinfo(title, msg)
        elif kind == "error":
            title, msg = payload
            messagebox.showerror(title, msg)
        elif kind == "login_finished":
            self._finish_login(*payload)
        elif kind == "reg_finished":
            self._finish_registration(payload)
        elif kind == "retry_finished":
            err, changed = payload
            self._finish_retry(err, changed)
        elif kind == "open_finished":
            stt, err = payload
            self._set_controls_busy(False)
            if err:
                messagebox.showerror(
                    "Không mở được form",
                    f"Không mở được form cho dòng #{stt}: {err}",
                )
        elif kind == "form_action_finished":
            stt, err = payload
            row = self.results_rows.get(stt)
            if row and row["status"] == STATUS_FORM_READY:
                for button in row["actions"][1:]:
                    button.configure(state="normal")
            if err:
                messagebox.showerror("Lỗi thao tác form", f"Dòng #{stt} — {err}")

    def _poll_ui_queue(self) -> None:
        if self._is_closing:
            return
        pending_logs = []
        deadline = perf_counter() + 0.008

        def flush_logs() -> None:
            if pending_logs:
                self.txt_log.insert("end", "".join(pending_logs))
                self.txt_log.see("end")
                pending_logs.clear()

        for _ in range(100):
            try:
                kind, payload = self.ui_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "log":
                timestamp = datetime.now().strftime("%H:%M:%S")
                pending_logs.append(f"[{timestamp}] {payload}\n")
            else:
                flush_logs()
                self._handle_ui_event(kind, payload)

            if perf_counter() >= deadline:
                break

        flush_logs()
        delay_ms = 150 if self.ui_queue.empty() else 10
        self._after_id = self.after(delay_ms, self._poll_ui_queue)

    def _finish_registration(self, failure: Optional[str]) -> None:
        self._set_controls_busy(False)
        if failure is not None:
            for row in self.results_rows.values():
                if row["status"] == STATUS_PENDING:
                    row["status"] = "Chưa gửi"
                    row["widgets"][0].configure(text=row["status"])
            messagebox.showerror("Phiên đăng ký bị gián đoạn", failure)
            return

        counts: Dict[str, int] = {}
        for row in self.results_rows.values():
            status = row["status"]
            counts[status] = counts.get(status, 0) + 1
        has_problems = any(
            status not in (STATUS_REGISTERED, STATUS_FORM_HANDOFF, STATUS_CONFIRMED, STATUS_EMAIL_EXISTS)
            for status in counts
        )
        states = (
            STATUS_REGISTERED, STATUS_FORM_HANDOFF, STATUS_TAB_CLOSED, STATUS_EMAIL_EXISTS, STATUS_TIMEOUT,
            STATUS_NOT_SUBMITTED, STATUS_SEND_UNKNOWN, STATUS_CHECK_MAIL, STATUS_CANCELLED, STATUS_FAILED,
        )
        lines = [f"{state} · {counts.pop(state, 0)}" for state in states]
        lines.extend(f"{state} · {amount}" for state, amount in counts.items())
        show_result = messagebox.showwarning if has_problems else messagebox.showinfo
        show_result("Kết quả phiên đăng ký", "\n".join(lines))

    def _finish_retry(self, failure: Optional[str], changed: list[int]) -> None:
        self._set_controls_busy(False)
        if failure is not None:
            messagebox.showerror("Lỗi kiểm tra thư lại", failure)
            return
        if changed:
            messagebox.showinfo(
                "Kiểm tra thư lại hoàn tất",
                f"Đã cập nhật kết quả cho {len(changed)} dòng: {changed}.",
            )
        else:
            messagebox.showinfo(
                "Chưa thấy thư mới",
                "Chưa tìm thấy thư xác nhận phù hợp trong INBOX. Bạn có thể thử lại sau.",
            )

    def _export_csv(self) -> None:
        emails = [
            row["email"]
            for _, row in sorted(self.results_rows.items())
            if row["status"] == STATUS_REGISTERED
        ]
        if not emails:
            messagebox.showinfo("Chưa có Gmail để xuất", "Chưa có tài khoản đăng ký thành công.", parent=self)
            return
        save_path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("Tệp CSV", "*.csv")],
            initialfile="gmail-da-dang-ky.csv", parent=self,
        )
        if not save_path:
            return
        try:
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["Gmail"])
                writer.writerows((email,) for email in emails)
        except OSError as exc:
            messagebox.showerror("Không xuất được CSV", str(exc), parent=self)
            return
        messagebox.showinfo("Đã xuất CSV", f"Đã lưu {len(emails)} Gmail vào\n{save_path}", parent=self)

    def shutdown(self) -> bool:
        self._is_closing = True
        if self._after_id is not None:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None
        return self.controller.shutdown()
