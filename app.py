import asyncio
import csv
import queue
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from time import perf_counter
import tkinter as tk
from tkinter import filedialog, messagebox
import webbrowser
import customtkinter as ctk

from hiragana_hook import ManualHiraganaHook, OCRHiraganaHook
from ninja_worker import JobConfig, NinjaAutomationRunner, PUBLIC_POST_FOUND, PUBLICATION_UNCONFIRMED
from ocr_cleanup import cleanup_ocr_images, cleanup_preference_file, save_cleanup_preference
from ocr_client import HiraganaOCRClient, OCRConfig, ensure_ocr_server_running
from parsers import parse_gmails, parse_posts_txt, validate_mapping
from registration_tab import RegistrationTab
from ui_style import (
    ACCENT, ACCENT_HOVER, BANNER, BORDER, CANVAS, INK, MUTED, SELECTED, STRIPE,
    SURFACE, TABLE_HEADER, bind_row_menu, card, empty_state, log_panel,
    primary_button, scroll_panel, secondary_button, section_title, show_row_menu,
    stop_button, textbox,
)

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class NinjaBlogApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Ninja Blog Automation v1.0.0")
        self.configure(fg_color=CANVAS)
        scale = self._get_window_scaling()
        width = min(1380, int(self.winfo_screenwidth() / scale) - 60)
        height = min(880, int(self.winfo_screenheight() / scale) - 100)
        self.geometry(f"{width}x{height}")
        self.minsize(980, 640)

        self.posts_map = {}
        self.results_rows = {}
        self.ui_queue: queue.Queue = queue.Queue()
        self.hiragana_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self.runner: NinjaAutomationRunner | None = None
        self.stop_requested: bool = False
        self.worker_thread: threading.Thread | None = None
        self._closing = False
        self.cleanup_on_exit = ctk.BooleanVar(self, value=cleanup_preference_file().is_file())

        self._build_layout()
        self.protocol("WM_DELETE_WINDOW", self._on_closing)
        self._poll_after_id = self.after(150, self._poll_ui_queue)

    def _build_layout(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, padx=18, pady=(14, 12), sticky="ew")
        top.grid_columnconfigure(1, weight=1)
        active_tab = self._active_tab = tk.StringVar(self, value="Tạo blog")
        self.tabview = ctk.CTkSegmentedButton(
            top, values=["Tạo blog", "Đăng ký tài khoản"], variable=active_tab,
            width=320, height=37, corner_radius=8, dynamic_resizing=False,
            fg_color=BORDER, selected_color=ACCENT,
            selected_hover_color=ACCENT_HOVER, unselected_color=("#4b607b", "#344862"),
            unselected_hover_color=("#385472", "#425979"),
            text_color="white", font=("Segoe UI", 13, "bold"),
        )
        self.tabview.grid(row=0, column=0, sticky="w")
        blog_toolbar = ctk.CTkFrame(top, fg_color="transparent")
        registration_toolbar = ctk.CTkFrame(top, fg_color="transparent")

        tab_blog = ctk.CTkFrame(self, fg_color="transparent")
        tab_blog.grid_columnconfigure(0, weight=4, minsize=365)
        tab_blog.grid_columnconfigure(1, weight=6, minsize=570)
        tab_blog.grid_rowconfigure(0, weight=1)
        self.registration_tab = RegistrationTab(self, toolbar_parent=registration_toolbar)

        tabs = {
            "Tạo blog": (tab_blog, blog_toolbar),
            "Đăng ký tài khoản": (self.registration_tab, registration_toolbar),
        }

        def select_tab(*_):
            selected = active_tab.get()
            for name, (panel, toolbar) in tabs.items():
                if name == selected:
                    panel.grid(row=1, column=0, padx=18, pady=(0, 16), sticky="nsew")
                    toolbar.grid(row=0, column=1, sticky="e")
                else:
                    panel.grid_remove()
                    toolbar.grid_remove()

        active_tab.trace_add("write", select_tab)
        self.tabview.set("Tạo blog")
        select_tab()

        # ================= CỘT TRÁI: CẤU HÌNH =================
        left = scroll_panel(tab_blog, label_text="Cấu hình chiến dịch")
        left.grid(row=0, column=0, padx=(0, 7), sticky="nsew")
        left.grid_columnconfigure(0, weight=1)

        # 1. Danh sách Gmail
        gmail_header = ctk.CTkFrame(left, fg_color="transparent")
        gmail_header.grid(row=0, column=0, padx=8, pady=(4, 3), sticky="ew")
        gmail_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            gmail_header, text="Danh sách Gmail", anchor="w", text_color=INK, font=("Segoe UI", 14, "bold")
        ).grid(row=0, column=0, sticky="w")
        self.lbl_gmail_count = ctk.CTkLabel(
            gmail_header, text="0 / 101", anchor="e", text_color=MUTED, font=("Segoe UI", 13, "bold")
        )
        self.lbl_gmail_count.grid(row=0, column=1, sticky="e")

        self.txt_gmails = textbox(left, height=125, placeholder="Mỗi dòng một địa chỉ Gmail")
        self.txt_gmails.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        self.txt_gmails.bind("<KeyRelease>", self._update_gmail_counter)
        self.txt_gmails.bind("<<Paste>>", lambda _event: self.after_idle(self._update_gmail_counter))

        # Mật khẩu dùng chung + Số luồng
        row_pw = ctk.CTkFrame(left, fg_color="transparent")
        row_pw.grid(row=2, column=0, padx=8, pady=4, sticky="ew")
        row_pw.grid_columnconfigure(0, weight=3)
        row_pw.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row_pw, text="Mật khẩu chung", anchor="w", text_color=INK).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(row_pw, text="Số phiên (1–10)", anchor="w", text_color=INK).grid(
            row=0, column=1, padx=(8, 0), sticky="w"
        )

        self.entry_password = ctk.CTkEntry(
            row_pw, show="*", placeholder_text="Nhập mật khẩu chung", border_color=BORDER, height=32
        )
        self.entry_password.grid(row=1, column=0, sticky="ew")

        self.entry_concurrency = ctk.CTkEntry(row_pw, width=104, border_color=BORDER, height=32)
        self.entry_concurrency.insert(0, "5")
        self.entry_concurrency.grid(row=1, column=1, padx=(8, 0), sticky="ew")

        # 2. Thiết lập Blog
        section_title(left, "Thiết lập blog").grid(row=3, column=0, padx=8, pady=(12, 2), sticky="ew")
        blog_fields = ctk.CTkFrame(left, fg_color="transparent")
        blog_fields.grid(row=4, column=0, padx=8, pady=(0, 4), sticky="ew")
        blog_fields.grid_columnconfigure((0, 1), weight=1, uniform="blog_fields")
        ctk.CTkLabel(blog_fields, text="Tên blog", anchor="w", text_color=INK).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(blog_fields, text="Subdomain", anchor="w", text_color=INK).grid(
            row=0, column=1, padx=(8, 0), sticky="w"
        )
        self.entry_blog_name = ctk.CTkEntry(blog_fields, border_color=BORDER, height=32)
        self.entry_blog_name.grid(row=1, column=0, sticky="ew")
        self.entry_subdomain = ctk.CTkEntry(blog_fields, border_color=BORDER, height=32)
        self.entry_subdomain.grid(row=1, column=1, padx=(8, 0), sticky="ew")

        # 3. Nội dung bài viết HTML
        section_title(left, "Nội dung bài viết (HTML)").grid(
            row=6, column=0, padx=8, pady=(12, 2), sticky="ew"
        )
        self.entry_post_title = ctk.CTkEntry(
            left, placeholder_text="Tiêu đề bài viết mặc định", border_color=BORDER, height=32
        )
        self.entry_post_title.grid(row=7, column=0, padx=8, pady=4, sticky="ew")

        self.html_mode_var = ctk.IntVar(value=1)
        mode_frame = ctk.CTkFrame(left, fg_color="transparent")
        mode_frame.grid(row=8, column=0, padx=8, pady=(6, 4), sticky="ew")

        ctk.CTkRadioButton(
            mode_frame,
            text="Dùng chung HTML",
            text_color=INK,
            variable=self.html_mode_var,
            value=1,
            command=self._toggle_html_mode,
        ).pack(side="left", padx=(0, 12), pady=2)
        ctk.CTkRadioButton(
            mode_frame,
            text="Nạp từ tệp TXT",
            text_color=INK,
            variable=self.html_mode_var,
            value=2,
            command=self._toggle_html_mode,
        ).pack(side="left", pady=2)

        # Frame Chế độ 1
        self.frame_mode1 = ctk.CTkFrame(left, fg_color="transparent")
        self.frame_mode1.grid(row=9, column=0, padx=8, pady=4, sticky="ew")
        self.frame_mode1.grid_columnconfigure(0, weight=1)
        self.txt_shared_html = textbox(self.frame_mode1, height=132, placeholder="Dán nội dung HTML tại đây")
        self.txt_shared_html.grid(row=0, column=0, sticky="ew")

        # Frame Chế độ 2
        self.frame_mode2 = ctk.CTkFrame(left, fg_color="transparent")
        self.frame_mode2.grid_columnconfigure(0, weight=1)
        self.btn_pick_txt = ctk.CTkButton(
            self.frame_mode2, text="Chọn tệp nội dung (.txt)", command=self._pick_txt_file
        )
        self.btn_pick_txt.grid(row=0, column=0, pady=4, sticky="ew")
        self.lbl_txt_status = ctk.CTkLabel(
            self.frame_mode2, text="Chưa chọn tệp nội dung.", anchor="w", wraplength=350
        )
        self.lbl_txt_status.grid(row=1, column=0, pady=2, sticky="ew")

        # 4. Tự động hóa giải Hiragana (OCR)
        section_title(left, "Xác thực Hiragana").grid(
            row=10, column=0, padx=8, pady=(14, 2), sticky="ew"
        )
        self.use_ocr_var = ctk.BooleanVar(value=True)
        self.chk_use_ocr = ctk.CTkCheckBox(
            left,
            text="Tự động giải qua OCR Hiragana cục bộ",
            variable=self.use_ocr_var,
            command=self._toggle_ocr_options,
        )
        self.chk_use_ocr.grid(row=11, column=0, padx=8, pady=4, sticky="w")

        self.frame_ocr = ctk.CTkFrame(left, fg_color="transparent")
        self.frame_ocr.grid(row=12, column=0, padx=8, pady=2, sticky="ew")
        self.frame_ocr.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(self.frame_ocr, text="Địa chỉ OCR Server:", anchor="w").grid(
            row=0, column=0, sticky="w"
        )
        self.entry_ocr_url = ctk.CTkEntry(self.frame_ocr, placeholder_text="http://127.0.0.1:8000")
        self.entry_ocr_url.insert(0, "http://127.0.0.1:8000")
        self.entry_ocr_url.grid(row=1, column=0, pady=2, sticky="ew")

        self.btn_check_ocr = ctk.CTkButton(
            self.frame_ocr,
            text="Kiểm tra kết nối OCR (/health)",
            fg_color="#2b7a4b",
            hover_color="#1e5c36",
            command=self._check_ocr_connection,
        )
        self.btn_check_ocr.grid(row=2, column=0, pady=4, sticky="ew")

        ctk.CTkCheckBox(
            left, text="Dọn ảnh Hiragana khi thoát (cả hai tab)", variable=self.cleanup_on_exit,
            command=self._save_cleanup_preference,
        ).grid(row=13, column=0, padx=8, pady=(8, 10), sticky="w")

        # ================= CỘT PHẢI: ĐIỀU KHIỂN & KẾT QUẢ =================
        right = ctk.CTkFrame(tab_blog, fg_color="transparent")
        right.grid(row=0, column=1, padx=(7, 0), sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=3, minsize=210)
        right.grid_rowconfigure(2, weight=2, minsize=150)

        # Thanh nút điều khiển
        self.btn_export = secondary_button(blog_toolbar, text="Xuất CSV", width=112, command=self._export_csv)
        self.btn_export.pack(side="left", padx=(0, 8))
        self.btn_stop = stop_button(
            blog_toolbar, text="Dừng lại", width=112, state="disabled", command=self._stop_job
        )
        self.btn_stop.pack(side="left", padx=(0, 8))
        self.btn_start = primary_button(blog_toolbar, text="Bắt đầu chạy", width=145, command=self._start_job)
        self.btn_start.pack(side="left")

        # Thanh điều phối Hiragana (tương phản rõ trên cả Light & Dark Mode)
        self.hiragana_bar = card(right, fg_color=BANNER)
        self.hiragana_bar.grid(row=0, column=0, pady=(0, 12), sticky="ew")
        self.hiragana_bar.grid_columnconfigure(0, weight=1)
        self.lbl_hiragana_wait = ctk.CTkLabel(
            self.hiragana_bar,
            text="Xác thực Hiragana: Sẵn sàng",
            font=("Segoe UI", 14, "bold"), text_color=INK,
            anchor="w",
            justify="left",
            wraplength=335,
        )
        self.lbl_hiragana_wait.grid(row=0, column=0, padx=14, pady=(8, 0), sticky="ew")
        self.lbl_hiragana_hint = ctk.CTkLabel(
            self.hiragana_bar, text="Ứng dụng sẽ tạm dừng khi cần nhập ký tự.",
            text_color=MUTED, anchor="w", justify="left", wraplength=330,
        )
        self.lbl_hiragana_hint.grid(row=1, column=0, padx=14, pady=(0, 8), sticky="ew")
        self.btn_hiragana_done = secondary_button(
            self.hiragana_bar, text="Đã gửi trên Chromium", state="disabled", command=self._confirm_manual_hiragana
        )
        self.btn_hiragana_done.grid(row=0, column=1, rowspan=2, padx=14, pady=10)

        # Bảng trạng thái
        progress = card(right)
        progress.grid(row=1, column=0, pady=(0, 12), sticky="nsew")
        progress.grid_columnconfigure(0, weight=1)
        progress.grid_rowconfigure(1, weight=1)
        section_title(progress, "Tiến độ tạo blog").grid(row=0, column=0, padx=14, pady=(10, 6), sticky="w")
        table = card(progress, corner_radius=7)
        table.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="nsew")
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(0, weight=1)
        self.table_frame = ctk.CTkScrollableFrame(
            table, fg_color=SURFACE, corner_radius=7, scrollbar_button_color=BORDER,
        )
        self.table_frame.grid(row=0, column=0, padx=1, pady=1, sticky="nsew")
        for column, (width, weight) in enumerate(((34, 0), (122, 2), (82, 1), (112, 2), (128, 3))):
            self.table_frame.grid_columnconfigure(column, minsize=width, weight=weight)
        self._render_table_header()
        self._empty_results = empty_state(table, "Chưa có kết quả", "Thêm Gmail và bấm Bắt đầu chạy.")
        self._empty_results.place(relx=0.5, rely=0.55, anchor="center")

        # Nhật ký chạy phía dưới
        logs, self.txt_log = log_panel(right)
        logs.grid(row=2, column=0, sticky="nsew")

    def _render_table_header(self):
        for column, text in enumerate(("#", "Gmail", "Domain", "Trạng thái", "Link blog")):
            ctk.CTkLabel(
                self.table_frame, text=text, width=0, height=34, padx=6, fg_color=TABLE_HEADER,
                anchor="center" if column == 0 else "w", text_color=INK, font=("Segoe UI", 12, "bold"),
            ).grid(row=0, column=column, sticky="ew")

    def _update_gmail_counter(self, _event=None):
        gmails, _ = parse_gmails(self.txt_gmails.get("1.0", "end"))
        self.lbl_gmail_count.configure(text=f"{len(gmails)} / 101")

    def _toggle_html_mode(self):
        if self.html_mode_var.get() == 1:
            self.frame_mode2.grid_forget()
            self.frame_mode1.grid(row=9, column=0, padx=8, pady=4, sticky="ew")
        else:
            self.frame_mode1.grid_forget()
            self.frame_mode2.grid(row=9, column=0, padx=8, pady=4, sticky="ew")

    def _toggle_ocr_options(self):
        if self.use_ocr_var.get():
            self.frame_ocr.grid(row=12, column=0, padx=8, pady=2, sticky="ew")
        else:
            self.frame_ocr.grid_forget()

    def _check_ocr_connection(self):
        url = self.entry_ocr_url.get().strip() or "http://127.0.0.1:8000"
        client = HiraganaOCRClient(OCRConfig(base_url=url))
        threading.Thread(target=self._async_check_ocr, args=(client,), daemon=True).start()

    def _async_check_ocr(self, client: HiraganaOCRClient):
        # Tự động thử bật server OCR nếu chưa chạy
        ensure_ocr_server_running(client.config.base_url)
        ok, info = client.check_health()
        if ok:
            inbox = info.get("inbox", "N/A")
            model = info.get("model", "N/A")
            server_cursor = client.get_server_latest_cursor()
            old_cursor = client.state_store.next_cursor
            client.sync_cursor()
            new_cursor = client.state_store.next_cursor
            cursor_info = f"{new_cursor}"
            if old_cursor > (server_cursor if server_cursor is not None else 0):
                cursor_info += f" (Đã tự động đặt lại từ {old_cursor} vì máy chủ ở mốc {server_cursor})"
            elif server_cursor is not None:
                cursor_info += f" (Khớp mốc máy chủ: {server_cursor})"

            key_pool = info.get("key_pool")
            pool_info = ""
            if key_pool:
                total_k = key_pool.get("total_keys", 1)
                models_list = ", ".join(key_pool.get("models", [model]))
                rem_rpd = key_pool.get("total_remaining_rpd", 0)
                details = key_pool.get("details", [])
                lines = []
                for d in details[:6]:
                    lines.append(f"  • {d.get('key_masked')} [{d.get('model')}]: RPM {d.get('rpm')}, RPD {d.get('rpd')} ({d.get('status')})")
                pool_info = (
                    f"\n\n[Quản lý Hồ bơi API Key & Model]\n"
                    f"- Tổng số API Key: {total_k}\n"
                    f"- Chuỗi Model dự phòng: {models_list}\n"
                    f"- Dung lượng còn lại hôm nay: {rem_rpd} lượt\n"
                    + "\n".join(lines)
                )

            msg = (
                f"Kết nối OCR thành công (HTTP 200 OK)!\n\n"
                f"- Địa chỉ: {client.config.base_url}\n"
                f"- Thư mục Inbox: {inbox}\n"
                f"- Mô hình hiện hành: {model}\n"
                f"- Mốc con trỏ kết quả: {cursor_info}"
                f"{pool_info}"
            )
            self.ui_queue.put(("info", ("OCR Sẵn sàng", msg)))
        else:
            err = info.get("error", "Không xác định")
            msg = (
                f"Không thể kết nối dịch vụ OCR tại {client.config.base_url}.\n\n"
                f"Chi tiết: {err}\n\n"
                f"Hãy khởi động server OCR bằng lệnh:\n"
                f".\\.venv\\Scripts\\python.exe ocr/server.py"
            )
            self.ui_queue.put(("error", ("Lỗi kết nối OCR", msg)))

    def _pick_txt_file(self):
        path = filedialog.askopenfilename(filetypes=[("Text files", "*.txt")])
        if not path:
            return
        posts_map, errors = parse_posts_txt(path)
        if errors:
            messagebox.showerror("Lỗi định dạng tệp TXT", "\n".join(errors[:10]))
            return
        self.posts_map = posts_map
        self.lbl_txt_status.configure(text=f"Đã nạp {len(posts_map)} bài viết từ:\n{path}")

    def _start_job(self):
        gmails, gmail_errors = parse_gmails(self.txt_gmails.get("1.0", "end"))
        if gmail_errors:
            messagebox.showerror("Lỗi danh sách Gmail", "\n".join(gmail_errors[:10]))
            return
        if not gmails:
            messagebox.showwarning("Thiếu dữ liệu", "Vui lòng nhập ít nhất 1 địa chỉ Gmail.")
            return

        password = self.entry_password.get()
        blog_name = self.entry_blog_name.get().strip()
        subdomain = self.entry_subdomain.get().strip()
        shared_title = self.entry_post_title.get().strip()

        if not password or not blog_name or not subdomain:
            messagebox.showwarning("Thiếu thông tin", "Vui lòng điền Mật khẩu chung, Tên blog và Subdomain.")
            return

        try:
            max_conc = int(self.entry_concurrency.get().strip())
        except ValueError:
            max_conc = 5

        mode = self.html_mode_var.get()
        shared_html = self.txt_shared_html.get("1.0", "end").strip()
        if mode == 1:
            if not shared_html or not shared_title:
                messagebox.showwarning("Thiếu nội dung", "Vui lòng nhập Tiêu đề mặc định và Nội dung HTML.")
                return
        else:
            missing = validate_mapping(len(gmails), self.posts_map)
            if missing:
                messagebox.showerror(
                    "Thiếu bài viết trong tệp TXT",
                    f"Các số thứ tự sau chưa có trong tệp TXT: {missing[:15]}",
                )
                return

        for row in self.results_rows.values():
            for widget in row["all_widgets"]:
                widget.destroy()
        self.results_rows.clear()
        for idx, email in enumerate(gmails, start=1):
            self._create_row(idx, email)

        config = JobConfig(
            gmails=gmails,
            shared_password=password,
            blog_name=blog_name,
            subdomain=subdomain,
            shared_title=shared_title,
            html_mode=mode,
            shared_html=shared_html,
            posts_map=self.posts_map,
            max_concurrent=max_conc,
        )

        self.stop_requested = False
        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        ocr_url = None
        if self.use_ocr_var.get():
            ocr_url = self.entry_ocr_url.get().strip() or "http://127.0.0.1:8000"
        self.worker_thread = threading.Thread(target=self._run_async_worker, args=(config, ocr_url), daemon=True)
        self.worker_thread.start()

    def _create_row(self, idx: int, email: str):
        self._empty_results.place_forget()
        background = STRIPE if idx % 2 == 0 else SURFACE
        labels = []
        cells = ((str(idx), 0), (email, 126), ("-", 78), ("Đang chờ", 112), ("", 145))
        for column, (value, wrap) in enumerate(cells):
            label = ctk.CTkLabel(
                self.table_frame, text=value, width=0, height=34, wraplength=wrap, padx=6,
                anchor="center" if column == 0 else "w", justify="left",
                fg_color=background, text_color=INK, cursor="hand2",
            )
            label.grid(row=idx, column=column, sticky="nsew")
            labels.append(label)
        self.results_rows[idx] = {
            "email": email,
            "domain": "-",
            "status": "Đang chờ",
            "url": "",
            "widgets": labels[2:],
            "all_widgets": labels,
        }
        bind_row_menu(labels, lambda event: self._show_row_context_menu(event, idx))

    def _copy_to_clipboard(self, text: str, message: str = ""):
        if not text:
            return
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            if message:
                self.ui_queue.put(("log", f"[Đã sao chép] {message}"))
        except Exception as exc:
            self.ui_queue.put(("log", f"Lỗi sao chép: {exc}"))

    def _flash_row(self, idx: int):
        row = self.results_rows.get(idx)
        if not row:
            return
        all_w = row.get("all_widgets", ())
        for w in all_w:
            try:
                w.configure(fg_color=SELECTED)
            except Exception:
                pass
        self.after(
            250,
            lambda: [
                w.configure(fg_color=STRIPE if idx % 2 == 0 else SURFACE)
                for w in all_w
                if hasattr(w, "winfo_exists") and w.winfo_exists()
            ],
        )

    def _copy_entire_row(self, idx: int):
        row = self.results_rows.get(idx)
        if not row:
            return
        self._flash_row(idx)
        status = row.get("status", "")
        if status == "DONE":
            status = "Hoàn tất"
        text = f"{idx}\t{row.get('email', '')}\t{row.get('domain', '-')}\t{status}\t{row.get('url', '')}".rstrip()
        self._copy_to_clipboard(text, f"Dòng #{idx}")

    def _copy_cell(self, idx: int, field: str):
        row = self.results_rows.get(idx)
        if not row:
            return
        val = (row.get(field) or "").strip()
        if not val:
            return
        self._flash_row(idx)
        label_map = {"url": f"Link Blog #{idx}", "email": f"Gmail #{idx}"}
        self._copy_to_clipboard(val, label_map.get(field, f"Dòng #{idx}"))

    def _show_row_context_menu(self, event, idx: int):
        row = self.results_rows.get(idx)
        if not row:
            return
        url = (row.get("url") or "").strip()
        options = [("Sao chép Gmail", lambda: self._copy_cell(idx, "email"))]
        if url:
            options.append(("Sao chép link blog", lambda: self._copy_cell(idx, "url")))
            options.append(("Mở link blog trên trình duyệt", lambda: webbrowser.open(url)))
        options.append((f"Sao chép toàn bộ dòng #{idx}", lambda: self._copy_entire_row(idx)))
        return show_row_menu(self, event, options)

    def _run_async_worker(self, config: JobConfig, ocr_url: str | None):
        failure = None
        try:
            self._run_job(config, ocr_url)
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            self.ui_queue.put(("log", f"Phiên chạy bị gián đoạn: {failure}"))
        finally:
            self.ui_queue.put(("finished", failure))

    def _run_job(self, config: JobConfig, ocr_url: str | None):
        async def wait_manual_hiragana(acc_idx: int, email: str):
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            self.ui_queue.put(("hiragana_wait", (acc_idx, email, loop, fut)))
            try:
                return await fut
            finally:
                self.ui_queue.put(("hiragana_done", acc_idx))

        hook = ManualHiraganaHook(wait_manual_hiragana)
        ocr_client = None

        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            if self.stop_requested:
                self.ui_queue.put(("log", "Phiên chạy đã bị hủy trước khi bắt đầu."))
                return

            if ocr_url:
                # Kiểm tra & tự động khởi chạy OCR Server nếu chưa chạy
                started_ok, start_msg = ensure_ocr_server_running(ocr_url)
                if started_ok:
                    self.ui_queue.put(("log", f"[OCR] {start_msg} ({ocr_url})"))
                else:
                    self.ui_queue.put(("log", f"[OCR Cảnh báo] {start_msg}. Sẽ thử kết nối hoặc fallback nhập tay."))

                if self.stop_requested:
                    self.ui_queue.put(("log", "Phiên chạy đã bị hủy sau khi kiểm tra OCR."))
                    return

                ocr_client = HiraganaOCRClient(OCRConfig(base_url=ocr_url))
                try:
                    ocr_client.start_polling(loop)
                except Exception as exc:
                    self.ui_queue.put(("log", f"Lỗi khởi động OCR polling ({exc})."))
                else:
                    hook = OCRHiraganaHook(
                        ocr_client=ocr_client,
                        fallback_hook=hook,
                        on_log=lambda msg: self.ui_queue.put(("log", msg)),
                    )
            else:
                self.ui_queue.put(("log", "Chạy ở chế độ xác thực Hiragana thủ công."))

            self.runner = NinjaAutomationRunner(
                config=config,
                hiragana_hook=hook,
                on_status=lambda i, e, d, s, u: self.ui_queue.put(("status", (i, e, d, s, u))),
                on_log=lambda msg: self.ui_queue.put(("log", msg)),
            )
            if self.stop_requested:
                self.runner.request_stop()
                return

            self.ui_queue.put(("log", "Đang khởi động Chromium…"))
            loop.run_until_complete(self.runner.run())
        finally:
            try:
                if ocr_client:
                    loop.run_until_complete(ocr_client.stop_polling())
            finally:
                asyncio.set_event_loop(None)
                loop.close()

    def _confirm_manual_hiragana(self):
        if not self.hiragana_waiters:
            return
        first_idx = next(iter(self.hiragana_waiters))
        loop, fut = self.hiragana_waiters.pop(first_idx)

        def confirm():
            if not fut.done():
                fut.set_result(None)

        loop.call_soon_threadsafe(confirm)
        self._refresh_hiragana_bar()

    def _refresh_hiragana_bar(self):
        if self.hiragana_waiters:
            first_idx = next(iter(self.hiragana_waiters))
            email = self.results_rows[first_idx]["email"]
            self.lbl_hiragana_wait.configure(text=f"Nhập thủ công cho [{first_idx}] {email}")
            self.lbl_hiragana_hint.configure(
                text="Nhập Hiragana và bấm 作成する trong Chromium, sau đó xác nhận tại đây."
            )
            self.btn_hiragana_done.configure(state="normal")
        else:
            self.lbl_hiragana_wait.configure(text="Xác thực Hiragana: Sẵn sàng")
            self.lbl_hiragana_hint.configure(text="Ứng dụng sẽ tạm dừng khi cần nhập ký tự.")
            self.btn_hiragana_done.configure(state="disabled")

    def _handle_ui_event(self, kind, payload):
        if kind == "status":
            idx, email, domain, status, url = payload
            row = self.results_rows.get(idx)
            if row and row["email"] == email:
                lbl_dom, lbl_status, lbl_url = row["widgets"]
                if row["domain"] != domain:
                    lbl_dom.configure(text=domain)
                if row["status"] != status:
                    lbl_status.configure(text="Hoàn tất" if status == "DONE" else status)
                if row["url"] != url:
                    lbl_url.configure(text=url, text_color=("#155bc2", "#93c1ff") if url else INK)
                row["domain"] = domain
                row["status"] = status
                row["url"] = url
        elif kind == "hiragana_wait":
            acc_idx, _email, loop, fut = payload
            self.hiragana_waiters[acc_idx] = (loop, fut)
            self._refresh_hiragana_bar()
        elif kind == "hiragana_done":
            self.hiragana_waiters.pop(payload, None)
            self._refresh_hiragana_bar()
        elif kind == "info":
            title, msg = payload
            messagebox.showinfo(title, msg)
        elif kind == "error":
            title, msg = payload
            messagebox.showerror(title, msg)
        elif kind == "finished":
            self._finish_job(payload)

    def _poll_ui_queue(self):
        pending_logs = []
        deadline = perf_counter() + 0.008

        def flush_logs():
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
        self._poll_after_id = self.after(delay_ms, self._poll_ui_queue)

    def _finish_job(self, failure: str | None):
        self.runner = None
        self.btn_start.configure(state="normal")
        self.btn_stop.configure(state="disabled")

        succeeded = failed = unfinished = unconfirmed = 0
        for row in self.results_rows.values():
            status = row["status"]
            if status in ("DONE", PUBLIC_POST_FOUND):
                succeeded += 1
            elif status == PUBLICATION_UNCONFIRMED:
                unconfirmed += 1
            elif status.startswith("LỖI") or status == "ĐÃ HỦY":
                failed += 1
            else:
                unfinished += 1
                row["status"] = "Phiên chạy bị gián đoạn" if failure is not None else "Chưa hoàn tất"
                row["widgets"][1].configure(text=row["status"])

        if failure is not None:
            messagebox.showerror("Phiên chạy bị gián đoạn", failure)
        elif unfinished or failed or unconfirmed:
            title = "Chưa hoàn tất toàn bộ" if unfinished else "Hoàn tất có lỗi"
            summary = f"Thành công: {succeeded}\nLỗi/Hủy: {failed}\nChưa hoàn tất: {unfinished}"
            if unconfirmed:
                title = PUBLICATION_UNCONFIRMED
                summary += f"\nCần đối chiếu trên blog trước khi đăng lại: {unconfirmed}"
            messagebox.showwarning(title, summary)
        else:
            messagebox.showinfo("Hoàn tất", f"Đã hoàn tất thành công {succeeded} blog!")

    def _stop_job(self):
        self.stop_requested = True
        self.btn_stop.configure(state="disabled")
        self.ui_queue.put(("log", "Đã nhận lệnh dừng từ người dùng. Đang yêu cầu dừng các tác vụ..."))
        if self.runner:
            self.runner.request_stop()

    def _export_csv(self):
        blog_urls = [
            row["url"]
            for _, row in sorted(self.results_rows.items())
            if row["url"]
        ]
        if not blog_urls:
            messagebox.showinfo("Chưa có URL blog", "Chưa có URL blog đã tạo để xuất.", parent=self)
            return
        save_path = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("Tệp CSV", "*.csv")],
            initialfile="url-blog-da-tao.csv", parent=self,
        )
        if not save_path:
            return
        try:
            with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["URL"])
                writer.writerows((url,) for url in blog_urls)
        except OSError as exc:
            messagebox.showerror("Không xuất được CSV", str(exc), parent=self)
            return
        messagebox.showinfo("Đã xuất CSV", f"Đã lưu {len(blog_urls)} URL blog vào\n{save_path}", parent=self)

    def _save_cleanup_preference(self):
        enabled = self.cleanup_on_exit.get()
        try:
            save_cleanup_preference(enabled)
        except OSError:
            self.cleanup_on_exit.set(not enabled)
            messagebox.showerror("Không lưu được tùy chọn", "Không thể lưu cài đặt dọn ảnh.", parent=self)

    def _on_closing(self):
        if self._closing:
            return
        self._closing = True
        self.stop_requested = True
        poll_id = getattr(self, "_poll_after_id", None)
        if poll_id is not None:
            try:
                self.after_cancel(poll_id)
            except Exception:
                pass
            self._poll_after_id = None
        if self.runner:
            self.runner.request_stop()
        try:
            registration_stopped = self.registration_tab.shutdown()
            if self.cleanup_on_exit.get():
                self._clean_images_on_exit(registration_stopped)
        finally:
            self.destroy()

    def _clean_images_on_exit(self, registration_stopped: bool):
        worker = self.worker_thread
        if worker is not None and worker.is_alive():
            worker.join(timeout=2)
        if not registration_stopped or (worker is not None and worker.is_alive()):
            messagebox.showinfo(
                "Dọn ảnh Hiragana", "Ảnh được giữ lại vì phiên làm việc vẫn đang dừng.", parent=self,
            )
            return
        try:
            cleanup_ocr_images(Path(__file__).resolve().parent)
        except (OSError, sqlite3.Error):
            messagebox.showwarning(
                "Dọn ảnh Hiragana", "Một số ảnh chưa dọn được. AutoBlog sẽ thử lại ở lần thoát sau.", parent=self,
            )


if __name__ == "__main__":
    app = NinjaBlogApp()
    app.mainloop()
