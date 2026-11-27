# Ninja Blog Automation Tool (AutoBlog) + OCR Hiragana AI

Ứng dụng Desktop tự động hóa toàn diện quy trình đăng nhập, khởi tạo blog và xuất bản bài viết HTML trên nền tảng Shinobi Blog (`ninja.co.jp`), tích hợp dịch vụ giải mã Captcha Hiragana tự động bằng mô hình AI Gemini (Flash / Flash-Lite) kết hợp OpenCV.

---

## 🌟 Tính năng nổi bật

1. **Quản lý 1–101 tài khoản linh hoạt:**
   - Dán trực tiếp danh sách Gmail vào giao diện, dùng chung 1 mật khẩu.
   - Tự động lọc dòng trống, trùng lặp và xác thực định dạng email chuẩn.
2. **Tối ưu tài nguyên với Playwright Chromium:**
   - Chạy cuốn chiếu 1 đến 10 phiên trình duyệt cô lập (`BrowserContext`) độc lập cookie/cache mà không lo tràn RAM.
3. **Tự động hóa giải mã Captcha Hiragana:**
   - Bắt tự động ảnh Captcha Hiragana sắc nét (kích thước đầy đủ).
   - Tự khởi chạy và đồng bộ dịch vụ OCR cục bộ (`http://127.0.0.1:8000`).
   - Sử dụng Google Gemini AI để giải mã và chuẩn hóa ký tự (bỏ dakuten/handakuten, đổi kana nhỏ sang cỡ thường thành đúng 6 ký tự).
   - Ghi tệp nguyên tử (`.part` ➔ `.png`) và theo dõi con trỏ tiến trình bền vững (`ocr_cursor.json`).
   - **Tự động chuyển chế độ giải tay (Fallback):** Nếu dịch vụ OCR chưa bật hoặc gặp ảnh lỗi, hệ thống tự động tạm dừng để người dùng nhập tay mà không làm gián đoạn tiến trình.
4. **Tự động chọn Subdomain & Tên miền:**
   - Điền subdomain, kích hoạt kiểm tra tính khả dụng, tự động bỏ qua tên miền bị khóa và chọn tuần tự các tên miền hợp lệ (`shinobi.jp`, `blog.ninja.jp`, ...).
5. **Đa dạng chế độ nạp bài viết HTML:**
   - **Chế độ 1 (Dùng chung):** Dán 1 đoạn mã HTML áp dụng cho tất cả blog.
   - **Chế độ 2 (Theo file TXT đánh số):** Ánh xạ 1-1 từng bài viết HTML theo số thứ tự tài khoản.
6. **Bảo mật tuyệt đối:**
   - Khóa API và tài khoản cá nhân được cấu hình qua file `.env`, tự động được loại trừ bởi `.gitignore`, đảm bảo an toàn khi chia sẻ mã nguồn lên GitHub.

---

## 📁 Cấu trúc thư mục dự án

```text
AutoBlog/
├── app.py                  # Giao diện Desktop GUI (CustomTkinter)
├── ninja_worker.py         # Bộ điều phối Playwright và quy trình tự động hóa
├── ninja_hiragana.py       # Bộ theo dõi & lưu trữ ảnh Hiragana Captcha
├── hiragana_hook.py        # Cổng giao tiếp Hiragana (Tự động OCR + Dự phòng nhập tay)
├── ocr_client.py           # Client kết nối HTTP tới OCR Server, quản lý cursor & ghi file
├── parsers.py              # Bộ phân tích danh sách Gmail và nội dung bài viết TXT
├── run.bat                 # Khởi chạy 1-click toàn bộ ứng dụng trên Windows
├── start_ocr.bat           # Khởi động độc lập OCR Hiragana Server (nếu cần)
├── requirements-runtime.txt # Thư viện để chạy ứng dụng desktop và dịch vụ OCR
├── requirements.txt        # Bản đầy đủ cho Streamlit, báo cáo và kiểm thử
├── .env.example            # Mẫu cấu hình biến môi trường và API Key
├── .gitignore              # Danh sách loại trừ tệp tạm, dữ liệu nhạy cảm
├── README.md               # Tài liệu hướng dẫn sử dụng và triển khai
│
├── ocr/                    # Phân hệ dịch vụ OCR Hiragana cục bộ
│   ├── server.py           # RESTful API Server (Starlette/FastAPI + Uvicorn)
│   ├── ocr_service.py      # Bộ xử lý nhận dạng ký tự qua Gemini AI
│   ├── folder_ocr.py       # Theo dõi thư mục inbox & hàng đợi SQLite (JobStore)
│   ├── formatter.py        # Chuẩn hóa ký tự Hiragana (6 ký tự, kana thường)
│   ├── preprocess.py       # Tiền xử lý ảnh (khử nhiễu, phân ngưỡng bằng OpenCV)
│   ├── config.py           # Cấu hình OCR, model, ngưỡng xử lý ảnh
│   ├── evaluate.py         # Đánh giá độ chính xác nhận dạng
│   ├── ocr_ui.py           # Giao diện Web kiểm tra OCR thủ công (Streamlit)
│   └── data/               # Mẫu ảnh kiểm thử và nhãn chuẩn (samples)
│
└── tests/                  # Bộ kiểm thử tự động (Unit & Integration Tests)
    └── test_ocr_integration.py  # 8 bài kiểm tra tích hợp Client - Server OCR
```

---

## 🚀 Hướng dẫn cài đặt & Chạy ứng dụng

### 1. Yêu cầu hệ thống
- Hệ điều hành: **Windows 10 / 11 (64-bit)**
- **Python 3.10 trở lên** (chọn *Add Python to PATH* khi cài đặt)

### 2. Cấu hình biến môi trường (`.env`)
1. Sao chép tệp `.env.example` thành `.env`:
   ```powershell
   copy .env.example .env
   ```
2. Mở tệp `.env` và điền khóa **Gemini API Key**:
   ```env
   GEMINI_API_KEY=AIzaSy...dien_khoa_cua_ban...
   GEMINI_MODEL=gemini-3.1-flash-lite
   OCR_HIRAGANA_URL=http://127.0.0.1:8000
   ```
   *(Nhận API Key miễn phí tại [Google AI Studio](https://aistudio.google.com/))*

### 3. Khởi chạy 1-Click (Khuyên dùng)
- Bấm đúp vào tệp **`run.bat`**.
- Lần đầu chạy, hệ thống sẽ tự động:
  1. Khởi tạo môi trường ảo `.venv`.
  2. Cài các thư viện chạy ứng dụng và dịch vụ OCR từ `requirements-runtime.txt`.
  3. Cài đặt trình duyệt Playwright Chromium.
  4. Khởi chạy giao diện chính của ứng dụng.

### 4. Cài đặt thủ công bằng dòng lệnh
```powershell
# Tạo và kích hoạt môi trường ảo
python -m venv .venv
.\.venv\Scripts\activate

# Cài đặt thư viện
pip install -r requirements.txt
playwright install chromium

# Chạy ứng dụng
python app.py
```

---

## Đăng nhập Gmail trong AutoBlog

Tab **Đăng ký tài khoản** hỗ trợ 2 cách kết nối hộp thư Gmail:

1. **App Password (16 ký tự — không cần Google Cloud):** Chọn **App Password (16 ký tự)**, nhập Gmail nhận thư xác nhận, bấm **Lấy App Password** để tạo mã 16 ký tự trong Google Account (yêu cầu tài khoản đã bật xác minh 2 bước), dán vào ô mật khẩu ứng dụng rồi bấm **Kết nối**.
2. **Đăng nhập Google (OAuth):** Chọn **Đăng nhập Google**, nhập Gmail rồi bấm **Đăng nhập**. Trình duyệt mở trang Google để bạn nhập mật khẩu, xác minh OTP nếu được hỏi và cấp quyền truy cập Gmail.

Ở cả hai cách, AutoBlog hiện **`Đã kết nối`** khi mở được hộp thư `INBOX`. Quyền đăng nhập chỉ giữ trong bộ nhớ đến khi đóng ứng dụng.

### Thiết lập Google lần đầu (chỉ dùng nếu chọn cách Đăng nhập Google OAuth)

1. Chọn dự án của bạn trong Google Cloud, bật Gmail API rồi mở **Google Auth Platform**. Với Gmail cá nhân, chọn **External**. Nếu ứng dụng ở chế độ **Testing**, thêm Gmail sẽ sử dụng vào **Audience → Test users**. [Cấu hình quyền truy cập](https://developers.google.com/workspace/guides/configure-oauth-consent)
2. Trong **Data Access**, thêm quyền IMAP `https://mail.google.com/`. Trong **Clients**, chọn **Create client → Desktop app** rồi tải JSON. [Tạo OAuth client](https://console.cloud.google.com/auth/clients)
3. Trong AutoBlog, bấm **Đăng nhập → Chọn tệp JSON**. AutoBlog lưu cấu hình ứng dụng vào `%LOCALAPPDATA%\AutoBlog\gmail_oauth_client.json` và mở Google ngay. Nếu cài AutoBlog cho nhiều người, có thể đặt tệp Desktop của ứng dụng cạnh `run.bat` với tên `gmail_oauth_client.json`. Khi đã có cấu hình, người dùng chỉ cần nhập Gmail và bấm **Đăng nhập**.

## 🛠️ Luồng hoạt động của tính năng OCR Hiragana

```
+------------------+         Lưu ảnh (.part -> .png)         +--------------------+
|  Playwright      | --------------------------------------> | ocr/inbox/         |
|  (ninja_worker)  |                                         +--------------------+
+------------------+                                                    |
         ^                                                   Lắng nghe tệp mới
         |                                                              v
+------------------+         Đọc /results?after=N            +--------------------+
|  ocr_client.py   | <-------------------------------------- | ocr/server.py      |
|  (OCRHiraganaHook|                                         | (FastAPI + Gemini) |
+------------------+                                         +--------------------+
```

1. **Chụp ảnh Captcha:** Playwright phát hiện phần tử ảnh `#authimg`, đợi ảnh tải xong (`naturalHeight > 30px`) và lưu vào bộ nhớ tạm.
2. **Ghi ảnh nguyên tử:** Client ghi vào `ocr/inbox/ninja-acc-1-1-<uuid>.png.part` rồi đổi tên thành `.png`.
3. **Nhận dạng bằng AI:** `ocr/server.py` tiếp nhận tệp, tiền xử lý qua OpenCV, gọi Gemini API nhận dạng và chuẩn hóa ký tự sang đúng 6 chữ Hiragana.
4. **Trả kết quả:** Client đọc kết quả qua API `/results`, xác thực độ dài, và tự động điền vào ô xác thực trên trang đăng ký Shinobi.
5. **Fallback an toàn:** Nếu OCR không nhận diện được hoặc dịch vụ tắt, ứng dụng tự động hiển thị nút xác nhận trên UI và đợi người dùng nhập tay.

Bật **Dọn ảnh Hiragana khi thoát (cả hai tab)** trong mục **Xác thực Hiragana** để xóa ảnh AutoBlog đã chụp và ảnh OCR đã xử lý trong các thư mục mặc định. Ảnh đang chờ xử lý và lịch sử OCR được giữ lại.

---

## 🧪 Chạy kiểm thử tự động

Từ thư mục gốc, cài bộ thư viện đầy đủ rồi chạy kiểm thử. Chromium cần được cài qua `run.bat` hoặc theo hướng dẫn cài thủ công ở trên. Các bài kiểm tra xuất bản dùng trang HTML cục bộ.

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -v
```

---

## 🔒 Bản quyền & Bảo mật

- Dự án phát triển phục vụ mục đích tự động hóa tác vụ quản trị blog cá nhân.
- Tuân thủ quy tắc bảo mật: Mọi dữ liệu tài khoản (`accounts.txt`, `*.csv`) và mã khóa bí mật (`.env`) đều được tự động bảo vệ khỏi việc commit lên kho mã nguồn công khai.
