import asyncio
import csv
import queue
import threading
from datetime import datetime
from tkinter import filedialog, messagebox
import customtkinter as ctk

from hiragana_hook import ManualHiraganaHook, OCRHiraganaHook
from ninja_worker import JobConfig, NinjaAutomationRunner
from ocr_client import HiraganaOCRClient, OCRConfig, ensure_ocr_server_running
from parsers import parse_gmails, parse_posts_txt, validate_mapping

ctk.set_appearance_mode("System")
ctk.set_default_color_theme("blue")


class NinjaBlogApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("Ninja Blog Automation v1.0.0")
        self.geometry("1220x780")
        self.minsize(1060, 700)

        self.posts_map = {}
        self.results_rows = {}
        self.ui_queue: queue.Queue = queue.Queue()
        self.hiragana_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self.runner: NinjaAutomationRunner | None = None
        self.stop_requested: bool = False
        self.worker_thread: threading.Thread | None = None

        self._build_layout()
        self.after(150, self._poll_ui_queue)

    def _build_layout(self):
        self.grid_columnconfigure(0, weight=4, minsize=390)
        self.grid_columnconfigure(1, weight=6, minsize=620)
        self.grid_rowconfigure(0, weight=1)

        # ================= CỘT TRÁI: CẤU HÌNH =================
        left = ctk.CTkScrollableFrame(self, label_text="Cấu hình chiến dịch")
        left.grid(row=0, column=0, padx=(12, 6), pady=12, sticky="nsew")
        left.grid_columnconfigure(0, weight=1)

        # 1. Danh sách Gmail
        self.lbl_gmail_count = ctk.CTkLabel(
            left, text="Danh sách Gmail (0 / 101)", anchor="w", font=("", 13, "bold")
        )
        self.lbl_gmail_count.grid(row=0, column=0, padx=8, pady=(6, 2), sticky="ew")

        self.txt_gmails = ctk.CTkTextbox(left, height=130)
        self.txt_gmails.grid(row=1, column=0, padx=8, pady=(0, 8), sticky="ew")
        self.txt_gmails.bind("<KeyRelease>", self._update_gmail_counter)

        # Mật khẩu dùng chung + Số luồng
        row_pw = ctk.CTkFrame(left, fg_color="transparent")
        row_pw.grid(row=2, column=0, padx=8, pady=4, sticky="ew")
        row_pw.grid_columnconfigure(0, weight=3)
        row_pw.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(row_pw, text="Mật khẩu chung:", anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(row_pw, text="Số luồng (1–10):", anchor="w").grid(row=0, column=1, padx=(8, 0), sticky="w")

        self.entry_password = ctk.CTkEntry(row_pw, show="*", placeholder_text="Nhập mật khẩu dùng chung...")
        self.entry_password.grid(row=1, column=0, sticky="ew")

        self.entry_concurrency = ctk.CTkEntry(row_pw, width=90)
        self.entry_concurrency.insert(0, "5")
        self.entry_concurrency.grid(row=1, column=1, padx=(8, 0), sticky="ew")

        # 2. Thiết lập Blog
        ctk.CTkLabel(left, text="Thiết lập Blog", anchor="w", font=("", 13, "bold")).grid(
            row=3, column=0, padx=8, pady=(12, 2), sticky="ew"
        )
        self.entry_blog_name = ctk.CTkEntry(left, placeholder_text="Tên blog (ツールの名前)")
        self.entry_blog_name.grid(row=4, column=0, padx=8, pady=4, sticky="ew")

        self.entry_subdomain = ctk.CTkEntry(left, placeholder_text="Subdomain (VD: myseoblog88)")
        self.entry_subdomain.grid(row=5, column=0, padx=8, pady=4, sticky="ew")

        # 3. Nội dung bài viết HTML
        ctk.CTkLabel(left, text="Nội dung bài viết (HTML)", anchor="w", font=("", 13, "bold")).grid(
            row=6, column=0, padx=8, pady=(12, 2), sticky="ew"
        )
        self.entry_post_title = ctk.CTkEntry(left, placeholder_text="Tiêu đề bài viết mặc định (タイトル)")
        self.entry_post_title.grid(row=7, column=0, padx=8, pady=4, sticky="ew")

        self.html_mode_var = ctk.IntVar(value=1)
        mode_frame = ctk.CTkFrame(left, fg_color="transparent")
        mode_frame.grid(row=8, column=0, padx=8, pady=(6, 4), sticky="ew")

        ctk.CTkRadioButton(
            mode_frame,
            text="Dùng chung 1 nội dung HTML cho tất cả blog",
            variable=self.html_mode_var,
            value=1,
            command=self._toggle_html_mode,
        ).pack(anchor="w", pady=2)
        ctk.CTkRadioButton(
            mode_frame,
            text="Nạp nội dung riêng cho từng blog từ tệp TXT",
            variable=self.html_mode_var,
            value=2,
            command=self._toggle_html_mode,
        ).pack(anchor="w", pady=2)

        # Frame Chế độ 1
        self.frame_mode1 = ctk.CTkFrame(left, fg_color="transparent")
        self.frame_mode1.grid(row=9, column=0, padx=8, pady=4, sticky="ew")
        self.frame_mode1.grid_columnconfigure(0, weight=1)
        self.txt_shared_html = ctk.CTkTextbox(self.frame_mode1, height=130)
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
        ctk.CTkLabel(left, text="Tự động hóa giải Hiragana (OCR)", anchor="w", font=("", 13, "bold")).grid(
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

        # ================= CỘT PHẢI: ĐIỀU KHIỂN & KẾT QUẢ =================
        right = ctk.CTkFrame(self)
        right.grid(row=0, column=1, padx=(6, 12), pady=12, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)

        # Thanh nút điều khiển
        ctrl = ctk.CTkFrame(right)
        ctrl.grid(row=0, column=0, padx=10, pady=10, sticky="ew")

        self.btn_start = ctk.CTkButton(ctrl, text="Bắt đầu chạy", fg_color="#1f6aa5", command=self._start_job)
        self.btn_start.pack(side="left", padx=6, pady=8)

        self.btn_stop = ctk.CTkButton(
            ctrl, text="Dừng lại", fg_color="#b33a3a", state="disabled", command=self._stop_job
        )
        self.btn_stop.pack(side="left", padx=6, pady=8)

        self.btn_export = ctk.CTkButton(ctrl, text="Xuất kết quả (.csv)", command=self._export_csv)
        self.btn_export.pack(side="right", padx=6, pady=8)

        # Thanh điều phối Hiragana (tương phản rõ trên cả Light & Dark Mode)
        self.hiragana_bar = ctk.CTkFrame(right, fg_color=("gray88", "#2b2b2b"))
        self.hiragana_bar.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        self.lbl_hiragana_wait = ctk.CTkLabel(
            self.hiragana_bar,
            text="Xác thực Hiragana: Sẵn sàng (tự tạm dừng khi cần nhập ký tự).",
            text_color=("gray15", "gray90"),
            anchor="w",
            justify="left",
            wraplength=375,
        )
        self.lbl_hiragana_wait.pack(side="left", padx=10, pady=8)
        self.btn_hiragana_done = ctk.CTkButton(
            self.hiragana_bar, text="Đã gửi trên Chromium", state="disabled", command=self._confirm_manual_hiragana
        )
        self.btn_hiragana_done.pack(side="right", padx=10, pady=8)

        # Bảng trạng thái
        self.table_frame = ctk.CTkScrollableFrame(right, label_text="Tiến độ xử lý")
        self.table_frame.grid(row=2, column=0, padx=10, pady=(0, 8), sticky="nsew")
        self.table_frame.grid_columnconfigure(1, weight=2)
        self.table_frame.grid_columnconfigure(3, weight=2)
        self.table_frame.grid_columnconfigure(4, weight=3)
        self._render_table_header()

        # Nhật ký chạy phía dưới
        self.txt_log = ctk.CTkTextbox(right, height=115)
        self.txt_log.grid(row=3, column=0, padx=10, pady=(0, 10), sticky="ew")

    def _render_table_header(self):
        headers = [("#", 35), ("Gmail", 0), ("Domain", 110), ("Trạng thái", 0), ("Link Blog", 0)]
        for col, (text, width) in enumerate(headers):
            lbl = ctk.CTkLabel(
                self.table_frame,
                text=text,
                width=width if width else 0,
                anchor="w" if col in (1, 3, 4) else "center",
                font=("", 12, "bold"),
            )
            lbl.grid(row=0, column=col, padx=4, pady=(2, 6), sticky="ew")

    def _update_gmail_counter(self, _event=None):
        gmails, _ = parse_gmails(self.txt_gmails.get("1.0", "end"))
        self.lbl_gmail_count.configure(text=f"Danh sách Gmail ({len(gmails)} / 101)")

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
            msg = (
                f"Kết nối OCR thành công (HTTP 200 OK)!\n\n"
                f"- Địa chỉ: {client.config.base_url}\n"
                f"- Thư mục Inbox: {inbox}\n"
                f"- Mô hình: {model}"
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

        for widget in self.table_frame.winfo_children():
            widget.destroy()
        self._render_table_header()
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
        lbl_idx = ctk.CTkLabel(self.table_frame, text=str(idx), width=35)
        lbl_mail = ctk.CTkLabel(self.table_frame, text=email, anchor="w")
        lbl_dom = ctk.CTkLabel(self.table_frame, text="-", width=110)
        lbl_status = ctk.CTkLabel(self.table_frame, text="Đang chờ", anchor="w")
        lbl_url = ctk.CTkLabel(self.table_frame, text="", anchor="w")

        lbl_idx.grid(row=idx, column=0, padx=4, pady=2)
        lbl_mail.grid(row=idx, column=1, padx=4, pady=2, sticky="ew")
        lbl_dom.grid(row=idx, column=2, padx=4, pady=2)
        lbl_status.grid(row=idx, column=3, padx=4, pady=2, sticky="ew")
        lbl_url.grid(row=idx, column=4, padx=4, pady=2, sticky="ew")

        self.results_rows[idx] = {
            "email": email,
            "domain": "-",
            "status": "Đang chờ",
            "url": "",
            "widgets": (lbl_dom, lbl_status, lbl_url),
        }

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
            self.lbl_hiragana_wait.configure(
                text=f"Nhập thủ công cho [{first_idx}] {email}\n"
                     "Trong Chromium, nhập Hiragana và bấm 作成する để gửi. "
                     "Khi trang chấp nhận, bấm “Đã gửi trên Chromium” ở đây."
            )
            self.btn_hiragana_done.configure(state="normal")
        else:
            self.lbl_hiragana_wait.configure(
                text="Xác thực Hiragana: Sẵn sàng (tự tạm dừng khi cần nhập ký tự)."
            )
            self.btn_hiragana_done.configure(state="disabled")

    def _poll_ui_queue(self):
        while not self.ui_queue.empty():
            kind, payload = self.ui_queue.get()
            if kind == "status":
                idx, _email, domain, status, url = payload
                row = self.results_rows.get(idx)
                if row:
                    row["domain"] = domain
                    row["status"] = status
                    row["url"] = url
                    lbl_dom, lbl_status, lbl_url = row["widgets"]
                    lbl_dom.configure(text=domain)
                    lbl_status.configure(text=status)
                    lbl_url.configure(text=url)
            elif kind == "log":
                self.txt_log.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {payload}\n")
                self.txt_log.see("end")
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
                self.runner = None
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")

                pending_count = sum(1 for r in self.results_rows.values() if r["status"] == "Đang chờ")
                error_count = sum(
                    1 for r in self.results_rows.values()
                    if r["status"].startswith("LỖI") or r["status"] == "ĐÃ HỦY" or r["status"] == "Phiên chạy bị gián đoạn"
                )
                success_count = sum(1 for r in self.results_rows.values() if r["status"] == "DONE")

                if payload is not None:
                    for row in self.results_rows.values():
                        if row["status"] == "Đang chờ":
                            row["status"] = "Phiên chạy bị gián đoạn"
                            row["widgets"][1].configure(text=row["status"])
                    messagebox.showerror("Phiên chạy bị gián đoạn", payload)
                elif pending_count > 0:
                    for row in self.results_rows.values():
                        if row["status"] == "Đang chờ":
                            row["status"] = "Chưa hoàn tất"
                            row["widgets"][1].configure(text=row["status"])
                    messagebox.showwarning(
                        "Chưa hoàn tất toàn bộ",
                        f"Kết thúc phiên chạy:\n- Thành công: {success_count}\n- Lỗi/Hủy: {error_count}\n- Chưa thực hiện: {pending_count}"
                    )
                else:
                    if error_count > 0:
                        messagebox.showwarning(
                            "Hoàn tất có lỗi",
                            f"Đã xử lý xong toàn bộ danh sách:\n- Thành công: {success_count}\n- Thất bại: {error_count}"
                        )
                    else:
                        messagebox.showinfo("Hoàn tất", f"Đã hoàn tất thành công {success_count} blog!")
        self.after(150, self._poll_ui_queue)

    def _stop_job(self):
        self.stop_requested = True
        self.btn_stop.configure(state="disabled")
        self.ui_queue.put(("log", "Đã nhận lệnh dừng từ người dùng. Đang yêu cầu dừng các tác vụ..."))
        if self.runner:
            self.runner.request_stop()

    def _export_csv(self):
        if not self.results_rows:
            messagebox.showinfo("Trống", "Chưa có dữ liệu kết quả để xuất.")
            return
        save_path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV files", "*.csv")])
        if not save_path:
            return
        with open(save_path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(["STT", "Gmail", "Domain", "Trạng thái", "Link Blog"])
            for idx, r in sorted(self.results_rows.items()):
                writer.writerow([idx, r["email"], r["domain"], r["status"], r["url"]])
        messagebox.showinfo("Đã xuất", f"Đã lưu kết quả ra tệp:\n{save_path}")


if __name__ == "__main__":
    app = NinjaBlogApp()
    app.mainloop()
