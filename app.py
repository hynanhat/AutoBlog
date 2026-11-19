import asyncio
import csv
import queue
import threading
from datetime import datetime
from tkinter import filedialog, messagebox
import customtkinter as ctk

from captcha_hook import ManualCaptchaHook
from ninja_worker import JobConfig, NinjaAutomationRunner
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
        self.captcha_waiters: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Future]] = {}
        self.runner: NinjaAutomationRunner | None = None
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

        # Thanh điều phối CAPTCHA (tương phản rõ trên cả Light & Dark Mode)
        self.captcha_bar = ctk.CTkFrame(right, fg_color=("gray88", "#2b2b2b"))
        self.captcha_bar.grid(row=1, column=0, padx=10, pady=(0, 8), sticky="ew")
        self.lbl_captcha_wait = ctk.CTkLabel(
            self.captcha_bar,
            text="Xác thực CAPTCHA: Sẵn sàng (tự tạm dừng khi cần nhập ký tự).",
            text_color=("gray15", "gray90"),
            anchor="w",
        )
        self.lbl_captcha_wait.pack(side="left", padx=10, pady=8)
        self.btn_captcha_done = ctk.CTkButton(
            self.captcha_bar, text="Xác nhận đã giải CAPTCHA", state="disabled", command=self._confirm_manual_captcha
        )
        self.btn_captcha_done.pack(side="right", padx=10, pady=8)

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

        password = self.entry_password.get().strip()
        blog_name = self.entry_blog_name.get().strip()
        subdomain = self.entry_subdomain.get().strip()
        shared_title = self.entry_post_title.get().strip()

        if not all([password, blog_name, subdomain]):
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

        self.btn_start.configure(state="disabled")
        self.btn_stop.configure(state="normal")
        self.worker_thread = threading.Thread(target=self._run_async_worker, args=(config,), daemon=True)
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

    def _run_async_worker(self, config: JobConfig):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def wait_manual_captcha(acc_idx: int, email: str):
            fut = loop.create_future()
            self.ui_queue.put(("captcha_wait", (acc_idx, email, loop, fut)))
            return await fut

        hook = ManualCaptchaHook(wait_manual_captcha)
        self.runner = NinjaAutomationRunner(
            config=config,
            captcha_hook=hook,
            on_status=lambda i, e, d, s, u: self.ui_queue.put(("status", (i, e, d, s, u))),
            on_log=lambda msg: self.ui_queue.put(("log", msg)),
        )
        loop.run_until_complete(self.runner.run())
        self.ui_queue.put(("finished", None))

    def _confirm_manual_captcha(self):
        if not self.captcha_waiters:
            return
        first_idx = next(iter(self.captcha_waiters))
        loop, fut = self.captcha_waiters.pop(first_idx)
        loop.call_soon_threadsafe(fut.set_result, None)
        self._refresh_captcha_bar()

    def _refresh_captcha_bar(self):
        if self.captcha_waiters:
            first_idx = next(iter(self.captcha_waiters))
            email = self.results_rows[first_idx]["email"]
            self.lbl_captcha_wait.configure(
                text=f"Đang chờ giải CAPTCHA trên Chrome cho [{first_idx}] {email} (còn {len(self.captcha_waiters)} phiên chờ)."
            )
            self.btn_captcha_done.configure(state="normal")
        else:
            self.lbl_captcha_wait.configure(
                text="Xác thực CAPTCHA: Sẵn sàng (tự tạm dừng khi cần nhập ký tự)."
            )
            self.btn_captcha_done.configure(state="disabled")

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
            elif kind == "captcha_wait":
                acc_idx, _email, loop, fut = payload
                self.captcha_waiters[acc_idx] = (loop, fut)
                self._refresh_captcha_bar()
            elif kind == "finished":
                self.btn_start.configure(state="normal")
                self.btn_stop.configure(state="disabled")
                messagebox.showinfo("Hoàn tất", "Đã hoàn tất xử lý danh sách tài khoản.")
        self.after(150, self._poll_ui_queue)

    def _stop_job(self):
        if self.runner:
            self.runner.request_stop()
            self.btn_stop.configure(state="disabled")

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