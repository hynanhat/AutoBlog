# Lấy kết quả OCR Hiragana cho ứng dụng khác

Ứng dụng nhận gọi `GET http://127.0.0.1:8000/results?after=0` để đọc kết quả đã hoàn tất. Với mỗi mục có `status = "done"`, lấy **`result.normalized_text`** làm đầu vào cho tác vụ cần chữ đã chuẩn hóa. **`filename`** cho biết chữ thuộc ảnh nào. Có thể xử lý từng kết quả ngay cả khi các ảnh còn lại trong lô vẫn đang chạy.

## Điều kiện kết nối

- Chạy mã lấy kết quả trên máy đang chạy OCR, hoặc trong môi trường truy cập được địa chỉ dịch vụ. Địa chỉ mặc định `127.0.0.1` luôn trỏ về máy của chương trình gọi. Nếu AI chạy trên đám mây, nó có thể viết mã để ứng dụng trên máy OCR thực hiện lệnh gọi.
- Phía nhận chỉ cần gửi HTTP GET và đọc JSON UTF-8. API cục bộ cho phép đọc trực tiếp. Khóa `GEMINI_API_KEY` được cấu hình ở phía dịch vụ OCR.
- Kiểm tra bằng `GET /health`. HTTP 200 và `status = "ok"` cho biết bộ theo dõi thư mục đang hoạt động. Trường `inbox` là đường dẫn đầu vào thực tế. Nếu cần khởi động dịch vụ, chạy `.\.venv\Scripts\python.exe server.py` tại `D:\OCR_Hiragana`.
- Nếu ứng dụng cũng tạo ảnh, lưu PNG/JPG/JPEG trực tiếp vào thư mục `inbox` (mặc định `D:\OCR_Hiragana\inbox`). Dùng tên duy nhất như `don-4821-trang-02.png` và lưu liên kết tên tệp với tác vụ gốc. Khi ghi ảnh theo từng phần, ghi vào tệp `.part` rồi đổi tên khi ghi xong. Một ứng dụng chỉ đọc kết quả cần quyền truy cập HTTP là đủ.

## Thử đọc bằng PowerShell

```powershell
$base = 'http://127.0.0.1:8000'
Invoke-RestMethod "$base/health"
$page = Invoke-RestMethod "$base/results?after=0&limit=100"
$page.items | Select-Object id, filename, status, @{Name='hiragana'; Expression={$_.result.normalized_text}}, error
$page.next_cursor
```

Ví dụ JSON dưới đây rút gọn các trường. Ảnh có `id = 10` hoàn tất sau ảnh có `id = 12`. Sau khi ghi nhận cả hai, ứng dụng đọc tiếp bằng `GET /results?after=8&limit=100`.

```json
{
  "items": [
    {
      "id": 12, "cursor": 7, "filename": "don-4821-trang-02.png", "status": "done",
      "result": {"text": "はひじぬせさ", "normalized_text": "はひしぬせさ", "warnings": []},
      "error": null
    },
    {
      "id": 10, "cursor": 8, "filename": "don-4820-trang-01.jpg", "status": "failed",
      "result": null, "error": "Tệp rỗng hoặc không phải ảnh PNG/JPEG đọc được."
    }
  ],
  "next_cursor": 8
}
```

| Ứng dụng cần làm gì? | Dữ liệu hoặc API cần dùng |
| --- | --- |
| Lấy chữ đã định dạng lại | `result.normalized_text` bỏ dấu đục, dấu bán đục và đổi kana nhỏ sang cỡ thường. |
| Giữ cách viết mà OCR đọc được | `result.text` giữ dấu và kana nhỏ sau khi lọc Hiragana. Có thể giữ `result.warnings` và `result.confidence_note` để đối chiếu. |
| Ghép kết quả vào đúng tác vụ | Dùng `filename` để tìm tác vụ gốc. Dùng `id` để nhận biết cùng một kết quả khi đọc lại từ cùng nguồn OCR. |
| Kiểm tra ảnh đã được tiếp nhận | `GET /jobs?after=0&limit=100` liệt kê theo `id`. Khi đã biết mã ảnh, gọi `GET /jobs/{id}` để xem `queued`, `processing`, `done` hoặc `failed`. HTTP 404 nghĩa là chưa có mã đó. |
| Nhận kết quả mới | `GET /results?after=N&limit=100` trả các mục có `cursor > N`, gồm cả ảnh lỗi. `limit` nhận giá trị từ 1 đến 100. Con trỏ của `/results` theo thứ tự hoàn tất, còn con trỏ của `/jobs` theo thứ tự tiếp nhận. |

## Cách đưa kết quả vào tác vụ

1. **Đọc vị trí đã lưu.** Mỗi ứng dụng nhận lưu một con trỏ riêng cho từng nguồn OCR. Dùng `0` cho lần đầu nếu muốn đọc cả lịch sử hiện có.
2. **Lấy một trang** bằng `GET /results?after=<con_trỏ_đã_lưu>&limit=100`. Nếu `items` rỗng, giữ con trỏ và chờ khoảng một giây trước khi gọi lại.
3. **Xử lý từng mục.** Với `done`, chuyển chữ và thông tin ảnh vào đúng tác vụ. Ví dụ, cập nhật trường Hiragana của trang 2 trong đơn 4821. Nếu nghiệp vụ yêu cầu đúng sáu ký tự, kiểm tra độ dài trường chữ sẽ dùng và chuyển mục chưa đạt sang bước kiểm tra. Với `failed`, lưu `id`, `filename` và `error`, rồi tiếp tục các mục còn lại.
4. **Chỉ lưu `next_cursor` sau khi toàn bộ trang đã được ghi nhận.** Nếu ứng dụng dừng giữa trang, lần chạy sau sẽ đọc lại trang đó. Khi lưu cùng cơ sở dữ liệu, có thể cập nhật kết quả nghiệp vụ và con trỏ trong một giao dịch. Khi gọi một hệ thống khác, dùng cặp nguồn OCR và `id` làm khóa chống xử lý trùng nếu hệ thống đích hỗ trợ.
5. **Khi có lỗi kết nối, timeout hoặc HTTP 5xx**, giữ con trỏ và thử lại sau một khoảng chờ. Với HTTP 400, kiểm tra `after` là số nguyên không âm và `limit` nằm trong khoảng hợp lệ. Lỗi OCR của một ảnh nằm trong mục `failed` của trang HTTP 200.

## Yêu cầu có thể giao trực tiếp cho AI lập trình

Thay phần trong ngoặc vuông bằng tác vụ thực tế rồi gửi đoạn sau cùng tài liệu này.

> Tích hợp OCR Hiragana vào [ứng dụng đích]. Khi ảnh hoàn tất, hãy [hành động cụ thể, ví dụ ghi chữ đã chuẩn hóa vào trường Hiragana của đơn tương ứng]. Triển khai mã chạy ở nơi truy cập được dịch vụ OCR, với URL cấu hình được và mặc định là `http://127.0.0.1:8000`. Đọc `/results` theo con trỏ, ghép ảnh với tác vụ bằng [quy tắc ánh xạ tên tệp], rồi dùng `result.normalized_text` cho mục `done`. Ghi nhận riêng mục `failed`. Lưu tiến độ sau khi tác vụ đích xác nhận đã nhận dữ liệu và bảo đảm đọc lại cùng `id` vẫn cho một kết quả nghiệp vụ duy nhất. Kiểm thử với một trang rỗng, một ảnh lỗi, các ảnh hoàn tất khác thứ tự gửi và một lần khởi động lại giữa trang.
